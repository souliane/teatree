"""Wiring tests for the idle reaper + queue drainer scanners (#2190, #44).

Covers dispatch routing (``local_stack.*`` → the mechanical handler, never an
agent, never the statusline), the kill-switch builders, and the
``build_default_jobs`` global registration.
"""

from unittest.mock import patch

from django.test import TestCase

from teatree.loop.dispatch import dispatch
from teatree.loop.scanners.base import ScanSignal


class DispatchRoutingTests(TestCase):
    """``local_stack.*`` signals route to mechanical handlers only."""

    def test_reap_idle_routes_to_reap_idle_stack_mechanical(self) -> None:
        signal = ScanSignal(kind="local_stack.reap_idle", summary="idle", payload={"worktree_id": 1})
        actions = dispatch([signal])
        assert len(actions) == 1
        assert actions[0].kind == "mechanical"
        assert actions[0].zone == "reap_idle_stack"

    def test_queue_acquire_routes_to_drain_mechanical(self) -> None:
        signal = ScanSignal(kind="local_stack.queue_acquire", summary="drain", payload={"queue_item_id": 1})
        actions = dispatch([signal])
        assert len(actions) == 1
        assert actions[0].kind == "mechanical"
        assert actions[0].zone == "drain_stack_queue_item"

    def test_local_stack_signals_never_render_to_statusline(self) -> None:
        for kind in ("local_stack.reap_idle", "local_stack.queue_acquire"):
            actions = dispatch([ScanSignal(kind=kind, summary="x", payload={})])
            assert all(a.kind != "statusline" for a in actions), kind


class BuilderTests(TestCase):
    """The builders honour the kill-switches and thread config."""

    def _cfg(self, settings: object) -> object:
        return type("Cfg", (), {"user": settings})()

    def test_reaper_builds_from_settings(self) -> None:
        from teatree.config import UserSettings  # noqa: PLC0415
        from teatree.loop.global_scanner_factories import _idle_stack_reaper_scanner  # noqa: PLC0415

        settings = UserSettings(idle_stack_idle_minutes=45, idle_stack_reaper_cadence_minutes=10)
        with patch("teatree.loop.global_scanner_factories.load_config", return_value=self._cfg(settings)):
            scanner = _idle_stack_reaper_scanner()
        assert scanner is not None
        assert scanner.idle_minutes == 45
        assert scanner.cadence_minutes == 10

    def test_reaper_kill_switch_returns_none(self) -> None:
        from teatree.config import UserSettings  # noqa: PLC0415
        from teatree.loop.global_scanner_factories import _idle_stack_reaper_scanner  # noqa: PLC0415

        settings = UserSettings(idle_stack_reaper_disabled=True)
        with patch("teatree.loop.global_scanner_factories.load_config", return_value=self._cfg(settings)):
            assert _idle_stack_reaper_scanner() is None

    def test_drainer_builds_from_settings(self) -> None:
        from teatree.config import UserSettings  # noqa: PLC0415
        from teatree.loop.global_scanner_factories import _local_stack_queue_drainer_scanner  # noqa: PLC0415

        with patch(
            "teatree.loop.global_scanner_factories.load_config",
            return_value=self._cfg(UserSettings()),
        ):
            assert _local_stack_queue_drainer_scanner() is not None

    def test_drainer_kill_switch_returns_none(self) -> None:
        from teatree.config import UserSettings  # noqa: PLC0415
        from teatree.loop.global_scanner_factories import _local_stack_queue_drainer_scanner  # noqa: PLC0415

        settings = UserSettings(local_stack_queue_disabled=True)
        with patch("teatree.loop.global_scanner_factories.load_config", return_value=self._cfg(settings)):
            assert _local_stack_queue_drainer_scanner() is None


class BuildDefaultJobsTests(TestCase):
    """``build_default_jobs`` wires (or omits) the global scanners."""

    def test_wires_reaper_and_drainer(self) -> None:
        from teatree.loop.global_scanner_factories import build_default_jobs  # noqa: PLC0415
        from teatree.loop.scanners import IdleStackReaperScanner, LocalStackQueueDrainerScanner  # noqa: PLC0415

        jobs = build_default_jobs()
        assert any(isinstance(j.scanner, IdleStackReaperScanner) and j.overlay == "" for j in jobs)
        assert any(isinstance(j.scanner, LocalStackQueueDrainerScanner) and j.overlay == "" for j in jobs)

    def test_omits_when_both_disabled(self) -> None:
        from teatree.loop.global_scanner_factories import build_default_jobs  # noqa: PLC0415
        from teatree.loop.scanners import IdleStackReaperScanner, LocalStackQueueDrainerScanner  # noqa: PLC0415

        with (
            patch("teatree.loop.global_scanner_factories._idle_stack_reaper_scanner", return_value=None),
            patch("teatree.loop.global_scanner_factories._local_stack_queue_drainer_scanner", return_value=None),
        ):
            jobs = build_default_jobs()
        assert not any(isinstance(j.scanner, IdleStackReaperScanner) for j in jobs)
        assert not any(isinstance(j.scanner, LocalStackQueueDrainerScanner) for j in jobs)
