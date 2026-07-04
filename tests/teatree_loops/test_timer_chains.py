"""teatree.loops.timer_chains — the self-rescheduling loop-timer tick body (#1796).

Integration-first against the real DB + the real ``django_tasks_db`` backend (so an
``enqueue`` lands a queryable ``run_after`` row), with the deadlined subprocess
tick stubbed so the five-step body is exercised without spawning a real tick.
"""

import datetime as dt

import django.test
import pytest
from django.utils import timezone

from teatree.core.models import Loop, Prompt
from teatree.loops import timer_chains

#: The production DB backend so an ``enqueue`` lands a real ``django_tasks_db`` row
#: (the suite default ``DummyBackend`` never touches the DB).
_DB_TASKS = {"default": {"BACKEND": "django_tasks_db.DatabaseBackend", "QUEUES": ["default", "loops"]}}


def _prompt(name: str = "demo-prompt") -> Prompt:
    prompt, _ = Prompt.objects.get_or_create(name=name, defaults={"body": "do x"})
    return prompt


@django.test.override_settings(USE_TZ=True)
class TestComputeSuccessorRunAfter(django.test.SimpleTestCase):
    """The conservative, crash-safe ``run_after`` rules for the next timer."""

    def test_chain_head_never_run_fires_now(self) -> None:
        now = timezone.now()
        row = Loop(name="h", delay_seconds=60, last_run_at=None)
        assert timer_chains.compute_successor_run_after(row, now) == now

    def test_overdue_interval_fires_now(self) -> None:
        now = timezone.now()
        row = Loop(name="o", delay_seconds=60, last_run_at=now - dt.timedelta(seconds=120))
        assert timer_chains.compute_successor_run_after(row, now) == now

    def test_future_interval_fires_at_anchor_plus_delay(self) -> None:
        now = timezone.now()
        last = now - dt.timedelta(seconds=10)
        row = Loop(name="f", delay_seconds=60, last_run_at=last)
        assert timer_chains.compute_successor_run_after(row, now) == last + dt.timedelta(seconds=60)

    def test_cadence_less_polls_on_the_60s_floor(self) -> None:
        now = timezone.now()
        row = Loop(name="c", delay_seconds=None, daily_at=None, last_run_at=None)
        got = timer_chains.compute_successor_run_after(row, now)
        assert got == now + dt.timedelta(seconds=timer_chains.CADENCE_LESS_POLL_FLOOR_SECONDS)

    def test_daily_fires_at_the_next_slot(self) -> None:
        now = timezone.now()
        row = Loop(name="d", daily_at=dt.time(8, 0), delay_seconds=None, last_run_at=None)
        got = timer_chains.compute_successor_run_after(row, now)
        assert got == row.next_run_at()


class TestComputeTickDeadline(django.test.SimpleTestCase):
    """``max(300s, 3 x cadence)``."""

    def test_short_cadence_floors_at_300s(self) -> None:
        assert timer_chains.compute_tick_deadline(Loop(name="s", delay_seconds=60)) == pytest.approx(300.0)

    def test_long_cadence_scales_to_three_times(self) -> None:
        assert timer_chains.compute_tick_deadline(Loop(name="l", delay_seconds=200)) == pytest.approx(600.0)

    def test_cadence_less_floors_at_300s(self) -> None:
        assert timer_chains.compute_tick_deadline(Loop(name="n", delay_seconds=None)) == pytest.approx(300.0)


@django.test.override_settings(USE_TZ=True, TASKS=_DB_TASKS)
class TestLoopTimerBody(django.test.TestCase):
    """The five-step tick body: dedup, successor-first, admission, tick, refinement."""

    def setUp(self) -> None:
        Loop.objects.all().delete()

    def _enable_inbox(self, **kwargs: object) -> Loop:
        # ``inbox`` is a real registered live-tick loop, so a real enabled + due row
        # is admitted by the unified verdict with no iter_loops patching.
        defaults: dict[str, object] = {"delay_seconds": 60, "enabled": True, "last_run_at": None}
        defaults.update(kwargs)
        return Loop.objects.create(name="inbox", script="src/teatree/loops/inbox/loop.py", **defaults)

    def test_self_dedup_stops_without_chaining(self) -> None:
        self._enable_inbox()
        timer_chains.enqueue_loop_timer("inbox", run_after=timezone.now())
        ran: list[str] = []
        with pytest.MonkeyPatch.context() as mp:
            mp.setattr(timer_chains, "run_deadlined_tick", lambda name, *, deadline: ran.append(name) or {})
            result = timer_chains.loop_timer.func("inbox")
        assert result["action"] == "deduped"
        assert ran == []  # no tick
        assert len(timer_chains.pending_loop_timers("inbox")) == 1  # no second timer enqueued

    def test_admitted_loop_runs_tick_and_refines_successor(self) -> None:
        row = self._enable_inbox()

        def _fake_tick(name: str, *, deadline: float) -> dict[str, object]:
            # A real tick's CAS bumps the anchor; simulate so the refinement reads a fresh one.
            Loop.objects.mark_run(name, timezone.now())
            return {"timed_out": False, "returncode": 0}

        with pytest.MonkeyPatch.context() as mp:
            mp.setattr(timer_chains, "run_deadlined_tick", _fake_tick)
            result = timer_chains.loop_timer.func("inbox")

        assert result["action"] == "ticked"
        pending = timer_chains.pending_loop_timers("inbox")
        assert len(pending) == 1  # exactly one successor
        row.refresh_from_db()
        # Refined to the fresh anchor + delay (post-tick), not the pre-tick "now".
        expected = row.last_run_at + dt.timedelta(seconds=60)
        assert abs((pending[0].run_after - expected).total_seconds()) < 2

    def test_held_loop_is_a_free_noop_with_idle_poll_successor(self) -> None:
        self._enable_inbox(enabled=False)  # disabled → not admitted
        ran: list[str] = []
        with pytest.MonkeyPatch.context() as mp:
            mp.setattr(timer_chains, "run_deadlined_tick", lambda name, *, deadline: ran.append(name) or {})
            result = timer_chains.loop_timer.func("inbox")
        assert result["action"] == "skipped"
        assert ran == []  # tick NOT run
        pending = timer_chains.pending_loop_timers("inbox")
        assert len(pending) == 1
        # Idle floor: a disabled interval loop polls no sooner than 60s out, never busy-spins.
        assert pending[0].run_after >= timezone.now() + dt.timedelta(seconds=timer_chains.IDLE_POLL_FLOOR_SECONDS - 2)

    def test_unknown_loop_does_not_chain(self) -> None:
        result = timer_chains.loop_timer.func("no-such-loop")
        assert result["action"] == "unknown"
        assert timer_chains.pending_loop_timers("no-such-loop") == []


class TestRunDeadlinedTick(django.test.SimpleTestCase):
    """The deadlined-subprocess + whole-group-kill contract, argv stubbed to shell tools."""

    def test_success_returns_returncode(self) -> None:
        with pytest.MonkeyPatch.context() as mp:
            mp.setattr(timer_chains, "_tick_argv", lambda name: ["true"])
            outcome = timer_chains.run_deadlined_tick("x", deadline=30)
        assert outcome == {"timed_out": False, "returncode": 0}

    def test_deadline_kills_the_process_group(self) -> None:
        started = timezone.now()
        with pytest.MonkeyPatch.context() as mp:
            mp.setattr(timer_chains, "_tick_argv", lambda name: ["sleep", "30"])
            outcome = timer_chains.run_deadlined_tick("x", deadline=0.3)
        elapsed = (timezone.now() - started).total_seconds()
        assert outcome["timed_out"] is True
        assert elapsed < 10  # the deadline fired and killed the group, not waited out the sleep
