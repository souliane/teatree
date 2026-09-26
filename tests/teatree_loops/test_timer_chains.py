"""teatree.loops.timer_chains — the self-rescheduling loop-timer tick body (#1796).

Integration-first against the real DB + the real ``django_tasks_db`` backend (so an
``enqueue`` lands a queryable ``run_after`` row), with the deadlined subprocess
tick stubbed so the five-step body is exercised without spawning a real tick.
"""

import datetime as dt
import types
import uuid

import django.test
import pytest
from django.tasks import TaskResultStatus
from django.utils import timezone

from teatree.core.models import Loop, Prompt
from teatree.loops import timer_chains
from teatree.loops.schedule_liveness import STUCK_GRACE_SECONDS
from teatree.loops.seed import DEFAULT_LOOPS


def _fire(name: str, *, task_id: uuid.UUID | None = None) -> dict:
    """Invoke the ``takes_context`` ``loop_timer`` body directly with a duck-typed context.

    The body reads only ``context.task_result.id`` (the id-tiebreak self-dedup); a
    ``SimpleNamespace`` supplies it without a real queue-claimed ``TaskContext``.
    """
    ctx = types.SimpleNamespace(task_result=types.SimpleNamespace(id=task_id or uuid.uuid4()))
    return timer_chains.loop_timer.func(ctx, name)


#: The production DB backend so an ``enqueue`` lands a real ``django_tasks_db`` row
#: (the suite default ``DummyBackend`` never touches the DB).
_DB_TASKS = {"default": {"BACKEND": "django_tasks_db.DatabaseBackend", "QUEUES": ["default", "loops", "cheap"]}}


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
    """Interval loops get ``max(300s, 3 x cadence)``; a ``daily_at`` loop gets its own deadline."""

    def test_short_cadence_floors_at_300s(self) -> None:
        assert timer_chains.compute_tick_deadline(Loop(name="s", delay_seconds=60)) == pytest.approx(300.0)

    def test_long_cadence_scales_to_three_times(self) -> None:
        assert timer_chains.compute_tick_deadline(Loop(name="l", delay_seconds=200)) == pytest.approx(600.0)

    def test_cadence_less_floors_at_300s(self) -> None:
        assert timer_chains.compute_tick_deadline(Loop(name="n", delay_seconds=None)) == pytest.approx(300.0)

    def test_daily_loop_gets_the_generous_daily_deadline_not_the_300s_floor(self) -> None:
        row = Loop(name="dly", delay_seconds=None, daily_at=dt.time(8, 0))
        assert timer_chains.compute_tick_deadline(row) == pytest.approx(timer_chains.DAILY_TICK_DEADLINE_SECONDS)
        assert timer_chains.DAILY_TICK_DEADLINE_SECONDS > timer_chains.MIN_TICK_DEADLINE_SECONDS

    def test_daily_deadline_wins_over_the_fallback_interval_every_script_loop_must_carry(self) -> None:
        # ``loop_script_requires_delay`` forces every script loop to store an interval, so
        # every SHIPPED daily loop carries one; keying the daily branch on its absence made
        # that branch unreachable and stretched the shipped 86400s fallback to a 72h ceiling.
        row = Loop(name="news", delay_seconds=86400, daily_at=dt.time(8, 0))
        assert timer_chains.compute_tick_deadline(row) == pytest.approx(timer_chains.DAILY_TICK_DEADLINE_SECONDS)

    def test_every_shipped_daily_seed_carries_the_interval_that_made_the_branch_unreachable(self) -> None:
        daily = [spec for spec in DEFAULT_LOOPS if spec.daily_at is not None]
        assert daily, "the shipped seed table no longer defines a daily loop"
        assert all(spec.delay_seconds for spec in daily)


@django.test.override_settings(USE_TZ=True, TASKS=_DB_TASKS)
class TestLoopTimerBody(django.test.TestCase):
    """The five-step tick body: dedup, successor-first, admission, tick, refinement."""

    def setUp(self) -> None:
        Loop.objects.all().delete()
        # Step 0 halts the chain when the FLEET admits nothing, so a second, always-admitted
        # loop keeps the fleet live while these body tests mask the loop under test — which
        # is also the pin that step 0 is fleet-scoped rather than per-loop.
        Loop.objects.create(
            name="dispatch", script="src/teatree/loops/dispatch/loop.py", delay_seconds=60, enabled=True
        )

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
            result = _fire("inbox")
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
            result = _fire("inbox")

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
        before = timezone.now()  # capture BEFORE the fire — the floor is anchored on fire-time, not assert-time
        with pytest.MonkeyPatch.context() as mp:
            mp.setattr(timer_chains, "run_deadlined_tick", lambda name, *, deadline: ran.append(name) or {})
            result = _fire("inbox")
        assert result["action"] == "skipped"
        assert ran == []  # tick NOT run
        pending = timer_chains.pending_loop_timers("inbox")
        assert len(pending) == 1
        # Idle floor: a disabled interval loop polls no sooner than 60s out, never busy-spins.
        assert pending[0].run_after >= before + dt.timedelta(seconds=timer_chains.IDLE_POLL_FLOOR_SECONDS - 2)

    def test_unknown_loop_does_not_chain(self) -> None:
        result = _fire("no-such-loop")
        assert result["action"] == "unknown"
        assert timer_chains.pending_loop_timers("no-such-loop") == []

    def test_faulted_tick_that_leaves_anchor_unmoved_floors_the_successor(self) -> None:
        # A crash before the CAS / connector outage / lost lease: the tick runs but
        # never moves ``last_run_at``, so the loop is still "due". Step 5 must floor
        # the successor to the idle poll — else ``compute_successor_run_after`` returns
        # ``now`` and the chain re-spawns a full Django subprocess every few seconds.
        self._enable_inbox(last_run_at=timezone.now() - dt.timedelta(seconds=120))  # overdue
        ticks: list[str] = []

        def _faulted_tick(name: str, *, deadline: float) -> dict[str, object]:
            ticks.append(name)  # runs, but does NOT mark_run → anchor stays put
            return {"timed_out": True, "returncode": None}

        before = timezone.now()
        with pytest.MonkeyPatch.context() as mp:
            mp.setattr(timer_chains, "run_deadlined_tick", _faulted_tick)
            result = _fire("inbox")

        assert result["action"] == "ticked"
        assert ticks == ["inbox"]  # exactly one tick — no duplicate spawned this fire
        pending = timer_chains.pending_loop_timers("inbox")
        assert len(pending) == 1  # one successor, not an unbounded storm
        floor = before + dt.timedelta(seconds=timer_chains.IDLE_POLL_FLOOR_SECONDS - 2)
        assert pending[0].run_after >= floor  # floored out, never re-fires at "now"

    def test_interval_fire_does_not_leave_an_immediately_ready_duplicate_successor(self) -> None:
        # Step 2 (successor-first) must NOT enqueue an already-due successor at ``now``:
        # a second ``loops`` executor would claim it and run a duplicate tick subprocess
        # while this tick is still in flight. Capture the successor AT tick time (after
        # step 2, before step 5's refinement).
        self._enable_inbox(last_run_at=timezone.now() - dt.timedelta(seconds=120))  # overdue
        successor_at_tick: list[dt.datetime] = []

        def _capture_at_tick(name: str, *, deadline: float) -> dict[str, object]:
            successor_at_tick.append(timer_chains.pending_loop_timers(name)[0].run_after)
            return {"timed_out": False, "returncode": 0}  # no mark_run — isolate step 2

        before = timezone.now()
        with pytest.MonkeyPatch.context() as mp:
            mp.setattr(timer_chains, "run_deadlined_tick", _capture_at_tick)
            _fire("inbox")

        assert successor_at_tick, "tick was not run"
        floor = before + dt.timedelta(seconds=timer_chains.IDLE_POLL_FLOOR_SECONDS - 2)
        assert successor_at_tick[0] >= floor  # step 2 floored → not immediately claimable

    def test_timed_out_tick_escalates_durably_once(self) -> None:
        # A tick SIGKILLed at its deadline already consumed its anchor, so its work is
        # lost until the next slot — that must surface LOUDLY (a DeferredQuestion), not
        # sit behind a lone logger.warning, and must not re-escalate every fire.
        from teatree.core.models.deferred_question import DeferredQuestion  # noqa: PLC0415

        self._enable_inbox(last_run_at=timezone.now() - dt.timedelta(seconds=120))

        def _timed_out(name: str, *, deadline: float) -> dict[str, object]:
            return {"timed_out": True, "returncode": None}

        marker = "loop-tick-timeout loop=inbox"
        with pytest.MonkeyPatch.context() as mp:
            mp.setattr(timer_chains, "run_deadlined_tick", _timed_out)
            _fire("inbox")
            assert DeferredQuestion.objects.filter(dedupe_marker=marker).count() == 1
            _fire("inbox")  # a second timeout must NOT spawn a second OPEN question
        assert DeferredQuestion.objects.filter(dedupe_marker=marker).count() == 1

    def test_tick_timeout_escalation_dedups_only_open_questions(self) -> None:
        # F6.12: the escalation dedups only OPEN (unanswered) questions. Two timeouts
        # while the question is pending collapse to one row; but once the user ANSWERS
        # it, a subsequent timeout raises a FRESH escalation — the old
        # `question__contains` dedup masked answered rows too, so a loop that kept
        # timing out after being answered fell silent forever.
        from teatree.core.models.deferred_question import DeferredQuestion  # noqa: PLC0415

        marker = "loop-tick-timeout loop=inbox"
        timer_chains._escalate_tick_timeout("inbox", deadline=300.0)
        timer_chains._escalate_tick_timeout("inbox", deadline=300.0)  # still open → deduped
        assert DeferredQuestion.objects.filter(dedupe_marker=marker).count() == 1

        first = DeferredQuestion.objects.get(dedupe_marker=marker)
        DeferredQuestion.consume(first.pk, answer="raise the deadline")
        timer_chains._escalate_tick_timeout("inbox", deadline=300.0)  # answered → re-escalate
        assert DeferredQuestion.objects.filter(dedupe_marker=marker).count() == 2
        assert DeferredQuestion.objects.filter(dedupe_marker=marker, answered_at__isnull=True).count() == 1

    def test_concurrent_running_duplicate_with_lower_id_dedups_this_fire(self) -> None:
        # Two racing RUNNING timers: the lower-id one survives, the higher-id one dedups —
        # so a slow anchor CAS that let a second executor claim a duplicate never runs two
        # concurrent ticks (the old READY-only self-dedup missed this).
        from django_tasks_db.models import DBTaskResult, normalize_uuid  # noqa: PLC0415

        self._enable_inbox()
        timer_chains.loop_timer.enqueue("inbox")
        row = timer_chains.pending_loop_timers("inbox")[0]
        DBTaskResult.objects.filter(id=row.id).update(status=TaskResultStatus.RUNNING)
        running_uuid = uuid.UUID(normalize_uuid(row.id))

        ran: list[str] = []
        with pytest.MonkeyPatch.context() as mp:
            mp.setattr(timer_chains, "run_deadlined_tick", lambda name, *, deadline: ran.append(name) or {})
            result = _fire("inbox", task_id=uuid.UUID(int=running_uuid.int + 1))  # my id outranked

        assert result["action"] == "deduped"
        assert ran == []  # the lower-id concurrent duplicate carries the chain, not this fire

    def test_lowest_id_fire_proceeds_past_a_higher_id_running_duplicate(self) -> None:
        from django_tasks_db.models import DBTaskResult, normalize_uuid  # noqa: PLC0415

        self._enable_inbox()
        timer_chains.loop_timer.enqueue("inbox")
        row = timer_chains.pending_loop_timers("inbox")[0]
        DBTaskResult.objects.filter(id=row.id).update(status=TaskResultStatus.RUNNING)
        running_uuid = uuid.UUID(normalize_uuid(row.id))

        ran: list[str] = []
        with pytest.MonkeyPatch.context() as mp:
            mp.setattr(
                timer_chains,
                "run_deadlined_tick",
                lambda name, *, deadline: ran.append(name) or {"timed_out": False, "returncode": 0},
            )
            result = _fire("inbox", task_id=uuid.UUID(int=running_uuid.int - 1))  # I am the lower id

        assert result["action"] == "ticked"
        assert ran == ["inbox"]  # the minimum-id fire is the survivor and runs the tick


@django.test.override_settings(USE_TZ=True, TASKS=_DB_TASKS)
class TestADedupNeverDropsTheChain(django.test.TestCase):
    """#4140: collapsing into another fire is a BET, and a lost bet stops the loop forever.

    A fire that deduped returned before enqueuing anything, on the assumption that the
    duplicate it deferred to would carry the chain. When that duplicate is a corpse — a
    RUNNING row whose worker died holding it — nothing ever enqueues again, and the loop
    keeps a recent anchor so every liveness surface reads healthy while it is dead.
    """

    def setUp(self) -> None:
        Loop.objects.all().delete()

    def _enable_inbox(self, **kwargs: object) -> Loop:
        defaults: dict[str, object] = {"delay_seconds": 60, "enabled": True, "last_run_at": None}
        defaults.update(kwargs)
        return Loop.objects.create(name="inbox", script="src/teatree/loops/inbox/loop.py", **defaults)

    def _running_duplicate(self, *, started_at: dt.datetime) -> uuid.UUID:
        from django.tasks import TaskResultStatus  # noqa: PLC0415 — deferred: heavy dep at call site
        from django_tasks_db.models import DBTaskResult, normalize_uuid  # noqa: PLC0415 — deferred: heavy dep

        timer_chains.loop_timer.enqueue("inbox")
        row = timer_chains.pending_loop_timers("inbox")[0]
        DBTaskResult.objects.filter(id=row.id).update(status=TaskResultStatus.RUNNING, started_at=started_at)
        return uuid.UUID(normalize_uuid(row.id))

    def test_deduping_to_a_live_duplicate_still_leaves_a_queued_successor(self) -> None:
        self._enable_inbox()
        duplicate = self._running_duplicate(started_at=timezone.now())

        with pytest.MonkeyPatch.context() as mp:
            mp.setattr(timer_chains, "run_deadlined_tick", lambda name, *, deadline: {})
            result = _fire("inbox", task_id=uuid.UUID(int=duplicate.int + 1))

        assert result["action"] == "deduped"
        assert timer_chains.pending_loop_timers("inbox"), "a dedup that queues nothing bets the chain on the other fire"

    def test_a_running_corpse_never_outranks_this_fire(self) -> None:
        row = self._enable_inbox()
        deadline = timer_chains.compute_tick_deadline(row) + STUCK_GRACE_SECONDS
        corpse = self._running_duplicate(started_at=timezone.now() - dt.timedelta(seconds=deadline + 60))

        ran: list[str] = []
        with pytest.MonkeyPatch.context() as mp:
            mp.setattr(
                timer_chains,
                "run_deadlined_tick",
                lambda name, *, deadline: ran.append(name) or {"timed_out": False, "returncode": 0},
            )
            result = _fire("inbox", task_id=uuid.UUID(int=corpse.int + 1))

        assert result["action"] == "ticked"
        assert ran == ["inbox"], "a dead worker's row is a corpse, never a duplicate that carries the chain"


@django.test.override_settings(USE_TZ=True, TASKS=_DB_TASKS)
class TestLoopTimerFleetVerdict(django.test.TestCase):
    """The fleet verdict terminates the chain at the timer source (C1).

    A posture switch that outlives a claimed timer must NOT let that timer re-enqueue its
    successor, or the chain ticks forever with nothing admitted. The check lives in the
    tick body so the posture kills the chain at its source, not only at the supervisor.
    """

    def setUp(self) -> None:
        Loop.objects.all().delete()

    def _inbox(self, **kwargs: object) -> Loop:
        defaults: dict[str, object] = {"delay_seconds": 60, "enabled": True, "last_run_at": None}
        defaults.update(kwargs)
        return Loop.objects.create(name="inbox", script="src/teatree/loops/inbox/loop.py", **defaults)

    def test_a_fleet_admitting_nothing_halts_the_chain_without_a_successor(self) -> None:
        self._inbox(enabled=False)  # the only loop, forced off — the fleet admits nothing
        ran: list[str] = []

        def _record_tick(name: str, *, deadline: float) -> dict[str, object]:
            ran.append(name)
            return {"timed_out": False, "returncode": 0}

        with pytest.MonkeyPatch.context() as mp:
            mp.setattr(timer_chains, "run_deadlined_tick", _record_tick)
            result = _fire("inbox")

        assert result["action"] == "halted"
        assert ran == []  # tick NOT run
        assert timer_chains.pending_loop_timers("inbox") == []  # NO successor — the chain terminates

    def test_an_admitting_fleet_keeps_the_chain_alive(self) -> None:
        # Anti-vacuity twin: the halt fires ONLY when the fleet admits nothing.
        self._inbox()

        def _fake_tick(name: str, *, deadline: float) -> dict[str, object]:
            Loop.objects.mark_run(name, timezone.now())
            return {"timed_out": False, "returncode": 0}

        with pytest.MonkeyPatch.context() as mp:
            mp.setattr(timer_chains, "run_deadlined_tick", _fake_tick)
            result = _fire("inbox")

        assert result["action"] == "ticked"
        assert len(timer_chains.pending_loop_timers("inbox")) == 1  # successor enqueued — chain lives
