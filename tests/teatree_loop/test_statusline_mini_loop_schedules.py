"""End-to-end mini-loop cadence read for the statusline loop line (#1400).

After the #2513 cutover :func:`teatree.loops.schedule.mini_loop_schedules`
derives its ``(name, next_fire_at, cadence_seconds)`` tuples from the DB
``Loop`` table (each row's ``enabled`` / cadence / ``last_run_at`` →
``next_run_at``) — the SAME live snapshot ``t3 loop list`` renders — so the
statusline's next-fire numbers stay in lockstep with the loop tick's own
cadence gate (:meth:`teatree.core.models.Loop.is_due`). Also covers the
injection seam that bridges this up-stack reader into the statusline without
violating the tach module graph.

The seeded production loops (migration 0078) live in the test DB, so the tests
that assert an exact schedule / chunk set clear the table first and create only
their own rows.
"""

import datetime as dt

import django.test
from django.utils import timezone

from teatree.core.models import Loop, LoopState, Prompt
from teatree.loop.statusline import mini_loops_anchor, set_mini_loop_schedules_reader
from teatree.loops.schedule import mini_loop_schedules


def _prompt() -> Prompt:
    prompt, _ = Prompt.objects.get_or_create(name="demo-schedule", defaults={"body": "x"})
    return prompt


def _make_loop(name: str, cadence: int, *, last_run_at: dt.datetime | None = None, enabled: bool = True) -> Loop:
    return Loop.objects.create(
        name=name,
        delay_seconds=cadence,
        prompt=_prompt(),
        enabled=enabled,
        last_run_at=last_run_at,
    )


@django.test.override_settings(USE_TZ=True)
class TestMiniLoopSchedulesFromLedger(django.test.TestCase):
    """``mini_loop_schedules`` derives each next-fire from the ``Loop`` row + cadence."""

    def test_next_fire_is_last_run_plus_cadence(self) -> None:
        Loop.objects.all().delete()
        fired_at = timezone.now() - dt.timedelta(seconds=60)
        _make_loop("dispatch", 300, last_run_at=fired_at)
        _make_loop("news", 3600, last_run_at=fired_at)
        schedules = {name: (next_fire, cadence) for name, next_fire, cadence in mini_loop_schedules()}
        assert schedules["dispatch"] == (fired_at + dt.timedelta(seconds=300), 300)
        assert schedules["news"] == (fired_at + dt.timedelta(seconds=3600), 3600)

    def test_never_run_loop_has_no_next_fire(self) -> None:
        Loop.objects.all().delete()
        _make_loop("inbox", 60)
        schedules = {name: next_fire for name, next_fire, _ in mini_loop_schedules()}
        assert schedules["inbox"] is None

    def test_disabled_loop_is_excluded(self) -> None:
        Loop.objects.all().delete()
        _make_loop("dispatch", 300)
        _make_loop("review", 300, enabled=False)
        names = [name for name, _, _ in mini_loop_schedules()]
        assert names == ["dispatch"]
        assert "review" not in names

    def test_results_sorted_by_name(self) -> None:
        Loop.objects.all().delete()
        _make_loop("ship", 300)
        _make_loop("audit", 300)
        _make_loop("inbox", 60)
        names = [name for name, _, _ in mini_loop_schedules()]
        assert names == ["audit", "inbox", "ship"]

    def test_paused_loop_is_excluded(self) -> None:
        # A PAUSED loop keeps Loop.enabled=True with a live cadence anchor, so
        # the pre-fix `if entry.enabled` filter kept it in the statusline
        # schedule with a countdown — masking that the tick skips it. The
        # schedule must mirror the tick's `loop_enabled` verdict.
        Loop.objects.all().delete()
        _make_loop("dispatch", 300, last_run_at=timezone.now())
        _make_loop("review", 300, last_run_at=timezone.now())
        LoopState.objects.pause("review")
        names = [name for name, _, _ in mini_loop_schedules()]
        assert "review" not in names
        # The peer loop proves the schedule is non-empty (not a blanket exclude).
        assert "dispatch" in names


@django.test.override_settings(USE_TZ=True)
class TestSeamRendersMiniLoopsOnStatusline(django.test.TestCase):
    """The injected reader makes every enabled cron appear with its own countdown."""

    def setUp(self) -> None:
        self.addCleanup(set_mini_loop_schedules_reader, None)

    def test_installed_reader_renders_relative_countdown(self) -> None:
        Loop.objects.all().delete()
        _make_loop("tickets", 300, last_run_at=timezone.now() - dt.timedelta(seconds=120))
        set_mini_loop_schedules_reader(mini_loop_schedules)
        chunks = mini_loops_anchor()
        # 120s elapsed of a 300s cadence → next fire in 180s → 3m.
        assert chunks == ["tickets 3m"], chunks

    def test_overdue_loop_reads_due(self) -> None:
        Loop.objects.all().delete()
        _make_loop("audit", 60, last_run_at=timezone.now() - dt.timedelta(hours=1))
        set_mini_loop_schedules_reader(mini_loop_schedules)
        chunks = mini_loops_anchor()
        assert chunks == ["audit due"], chunks

    def test_no_reader_installed_renders_nothing(self) -> None:
        set_mini_loop_schedules_reader(None)
        assert mini_loops_anchor() == []


@django.test.override_settings(USE_TZ=True)
class TestMiniLoopCadenceMatchesMasterGate(django.test.TestCase):
    """The statusline next-fire stays in lockstep with the loop tick gate.

    The same ``last_run + cadence`` boundary :meth:`Loop.is_due` uses to decide
    whether the master fires a loop is the boundary the statusline counts down to:
    when the master would fire (boundary in the past) the statusline reads ``due``.
    Both read the ONE ledger — the ``Loop`` row's ``last_run_at`` — so they agree
    by construction.
    """

    def setUp(self) -> None:
        self.addCleanup(set_mini_loop_schedules_reader, None)

    def test_due_when_master_gate_would_fire(self) -> None:
        Loop.objects.all().delete()
        now = timezone.now()
        fired_at = now - dt.timedelta(seconds=400)
        row = _make_loop("ship", 300, last_run_at=fired_at)
        set_mini_loop_schedules_reader(mini_loop_schedules)
        chunks = mini_loops_anchor()
        assert row.is_due(now) is True
        assert chunks == ["ship due"], chunks
