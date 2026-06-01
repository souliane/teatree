"""End-to-end mini-loop cadence read for the statusline loop line (#1400).

Exercises :func:`teatree.loops.schedule.mini_loop_schedules` against a real
:class:`~teatree.core.models.MiniLoopMarker` ledger, the real mini-loop
registry, and a real :class:`~teatree.loops.config.LoopsConfig` — no stub of
the DB+config read — so the statusline's next-fire numbers stay in lockstep
with the orchestrator's own cadence gate
(:func:`teatree.loops.gating.elapsed_and_enabled`). Also covers the
injection seam that bridges this up-stack reader into the statusline without
violating the tach module graph.
"""

import datetime as dt
from contextlib import AbstractContextManager
from pathlib import Path
from unittest.mock import patch

import django.test
from django.utils import timezone

from teatree.core.models.mini_loop_marker import MiniLoopMarker
from teatree.loop.statusline import mini_loops_anchor, set_mini_loop_schedules_reader
from teatree.loops.base import MiniLoop
from teatree.loops.config import LoopsConfig
from teatree.loops.schedule import mini_loop_schedules


def _stub_loop(name: str, cadence: int) -> MiniLoop:
    return MiniLoop(name=name, default_cadence_seconds=cadence, build_jobs=lambda **_: [])


def _default_config() -> AbstractContextManager[object]:
    """Patch ``LoopsConfig.load`` so the host's ``~/.teatree.toml`` never leaks in."""
    return patch.object(LoopsConfig, "load", classmethod(lambda cls, path=None: cls()))


class TestMiniLoopSchedulesFromLedger(django.test.TestCase):
    """``mini_loop_schedules`` derives each next-fire from the ledger + cadence."""

    def test_next_fire_is_last_fired_plus_cadence(self) -> None:
        loops = (_stub_loop("dispatch", 300), _stub_loop("news", 3600))
        fired_at = timezone.now() - dt.timedelta(seconds=60)
        MiniLoopMarker.objects.mark_fired("dispatch", fired_at)
        MiniLoopMarker.objects.mark_fired("news", fired_at)
        with patch("teatree.loops.schedule.iter_loops", return_value=loops), _default_config():
            schedules = dict(mini_loop_schedules())
        assert schedules["dispatch"] == fired_at + dt.timedelta(seconds=300)
        assert schedules["news"] == fired_at + dt.timedelta(seconds=3600)

    def test_never_fired_loop_has_no_next_fire(self) -> None:
        loops = (_stub_loop("inbox", 60),)
        with patch("teatree.loops.schedule.iter_loops", return_value=loops), _default_config():
            schedules = dict(mini_loop_schedules())
        assert schedules["inbox"] is None

    def test_disabled_loop_is_excluded(self) -> None:
        loops = (_stub_loop("dispatch", 300), _stub_loop("review", 300))
        with (
            patch("teatree.loops.schedule.iter_loops", return_value=loops),
            patch(
                "teatree.loops.config.LoopsConfig.is_enabled",
                side_effect=lambda loop: loop.name != "review",
            ),
        ):
            names = [name for name, _ in mini_loop_schedules()]
        assert names == ["dispatch"]
        assert "review" not in names

    def test_results_sorted_by_name(self) -> None:
        loops = (_stub_loop("ship", 300), _stub_loop("audit", 300), _stub_loop("inbox", 60))
        with patch("teatree.loops.schedule.iter_loops", return_value=loops), _default_config():
            names = [name for name, _ in mini_loop_schedules()]
        assert names == ["audit", "inbox", "ship"]


class TestSeamRendersMiniLoopsOnStatusline(django.test.TestCase):
    """The injected reader makes every enabled cron appear with its own countdown."""

    def setUp(self) -> None:
        self.addCleanup(set_mini_loop_schedules_reader, None)

    def test_installed_reader_renders_relative_countdown(self) -> None:
        loops = (_stub_loop("tickets", 300),)
        MiniLoopMarker.objects.mark_fired("tickets", timezone.now() - dt.timedelta(seconds=120))
        with patch("teatree.loops.schedule.iter_loops", return_value=loops), _default_config():
            set_mini_loop_schedules_reader(mini_loop_schedules)
            chunks = mini_loops_anchor()
        # 120s elapsed of a 300s cadence → next fire in 180s → 3m.
        assert chunks == ["tickets 3m"], chunks

    def test_overdue_loop_reads_due(self) -> None:
        loops = (_stub_loop("audit", 60),)
        MiniLoopMarker.objects.mark_fired("audit", timezone.now() - dt.timedelta(hours=1))
        with patch("teatree.loops.schedule.iter_loops", return_value=loops), _default_config():
            set_mini_loop_schedules_reader(mini_loop_schedules)
            chunks = mini_loops_anchor()
        assert chunks == ["audit due"], chunks

    def test_no_reader_installed_renders_nothing(self) -> None:
        set_mini_loop_schedules_reader(None)
        assert mini_loops_anchor() == []


class TestMiniLoopCadenceMatchesGate(django.test.TestCase):
    """The statusline next-fire stays in lockstep with the orchestrator gate.

    The same ``last_fired_at + cadence`` boundary the gate uses to decide
    ``should_fire`` is the boundary the statusline counts down to: when the
    gate would fire (boundary in the past) the statusline reads ``due``.
    """

    def setUp(self) -> None:
        self.addCleanup(set_mini_loop_schedules_reader, None)

    def test_due_when_gate_would_fire(self) -> None:
        from teatree.loops.gating import elapsed_and_enabled  # noqa: PLC0415

        loop = _stub_loop("ship", 300)
        now = timezone.now()
        MiniLoopMarker.objects.mark_fired("ship", now - dt.timedelta(seconds=400))
        decision = elapsed_and_enabled(LoopsConfig(), loop, now)
        with patch("teatree.loops.schedule.iter_loops", return_value=(loop,)), _default_config():
            set_mini_loop_schedules_reader(mini_loop_schedules)
            chunks = mini_loops_anchor()
        assert decision.should_fire is True
        assert chunks == ["ship due"], chunks


def test_config_loader_degrades_to_defaults_on_missing_file(tmp_path: Path) -> None:
    """Sanity guard: the config loader degrades to defaults on a missing file."""
    cfg = LoopsConfig.load(path=tmp_path / "absent.toml")
    assert cfg.enabled is True
