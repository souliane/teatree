"""teatree.loops.timer_reconciler — the deterministic zero-token chain reconciler (#1796).

Integration-first against the real DB + ``django_tasks_db`` backend: the reconciler
adds a missing head, prunes a surplus timer, repairs a stranded RUNNING chain, and
deletes a disabled/unknown loop's timers — all without dispatching anything.
"""

import datetime as dt
import os
import tempfile
import types
from pathlib import Path
from unittest import mock

import django.test
from django.tasks import TaskResultStatus
from django.utils import timezone
from django_tasks_db.models import DBTaskResult, get_date_max

from teatree.core.claim_liveness import driving
from teatree.core.models import Loop, LoopState, Mode, ModeOverride, Session, Task, Ticket
from teatree.core.tasks import execute_task
from teatree.loops import off_live_tick_driver, timer_chains, timer_reconciler
from teatree.loops.timer_reconciler import reap_stuck_runs
from tests._t3_master_env import worker_owns_t3_master
from tests.teatree_core.test_claim_liveness import _READER_NS, pinned_reader_namespace
from tests.teatree_loops.mode_scenarios import LOOP, ModeWithoutOverrideMixin

_DB_TASKS = {"default": {"BACKEND": "django_tasks_db.DatabaseBackend", "QUEUES": ["default", "loops", "cheap"]}}
_READY_HEADLESS_JOB = {"task_path": execute_task.module_path, "status": TaskResultStatus.READY}
#: A preset of the test's own, so nothing here depends on the seeded production modes.
_PRESET = "forced-on-4185"
#: A worker pid that is not the recorded singleton holder — a replaced worker, so gone.
_REPLACED_WORKER_PID = 424242


@django.test.override_settings(USE_TZ=True, TASKS=_DB_TASKS)
class TestEnsureLoopTimers(django.test.TestCase):
    def setUp(self) -> None:
        Loop.objects.all().delete()
        DBTaskResult.objects.all().delete()

    def _enable(self, name: str = "inbox", **kwargs: object) -> Loop:
        defaults: dict[str, object] = {
            "delay_seconds": 60,
            "enabled": True,
            "last_run_at": None,
            "override_reason": "test override",
        }
        defaults.update(kwargs)
        return Loop.objects.create(name=name, script=f"src/teatree/loops/{name}/loop.py", **defaults)

    def test_adds_a_missing_chain_head(self) -> None:
        self._enable()
        counts = timer_reconciler.ensure_loop_timers()
        assert counts["added"] == 1
        assert len(timer_chains.pending_loop_timers("inbox")) == 1

    def test_idempotent_no_duplicate_head(self) -> None:
        self._enable()
        timer_reconciler.ensure_loop_timers()
        counts = timer_reconciler.ensure_loop_timers()
        assert counts["added"] == 0
        assert len(timer_chains.pending_loop_timers("inbox")) == 1

    def test_prunes_a_surplus_timer_keeping_the_earliest(self) -> None:
        self._enable()
        now = timezone.now()
        timer_chains.enqueue_loop_timer("inbox", run_after=now + dt.timedelta(seconds=10))
        timer_chains.enqueue_loop_timer("inbox", run_after=now + dt.timedelta(seconds=99))
        counts = timer_reconciler.ensure_loop_timers()
        assert counts["pruned"] == 1
        pending = timer_chains.pending_loop_timers("inbox")
        assert len(pending) == 1
        assert pending[0].run_after == now + dt.timedelta(seconds=10)  # earliest kept

    def test_deletes_a_force_off_loops_timer(self) -> None:
        self._enable(enabled=False)
        timer_chains.enqueue_loop_timer("inbox", run_after=timezone.now())
        counts = timer_reconciler.ensure_loop_timers()
        assert counts["pruned"] == 1
        assert timer_chains.pending_loop_timers("inbox") == []

    def test_deletes_an_unknown_loops_timer(self) -> None:
        timer_chains.enqueue_loop_timer("ghost", run_after=timezone.now())
        counts = timer_reconciler.ensure_loop_timers()
        assert counts["pruned"] == 1
        assert timer_chains.pending_loop_timers("ghost") == []

    def test_repairs_a_stranded_running_chain(self) -> None:
        self._enable()
        timer_chains.enqueue_loop_timer("inbox", run_after=timezone.now())
        [timer] = timer_chains.pending_loop_timers("inbox")
        # Simulate a worker that claimed the timer then died long past the deadline.
        DBTaskResult.objects.filter(id=timer.id).update(
            status=TaskResultStatus.RUNNING,
            started_at=timezone.now() - dt.timedelta(hours=1),
        )
        counts = timer_reconciler.ensure_loop_timers()
        assert counts["repaired"] == 1  # stranded RUNNING cleaned up
        assert counts["added"] == 1  # fresh head created
        assert timer_chains.running_loop_timers("inbox") == []
        assert len(timer_chains.pending_loop_timers("inbox")) == 1

    def test_off_live_tick_loop_gets_no_chain(self) -> None:
        # ``dream`` is off_live_tick — the driver chain fires its tick command, never a timer.
        Loop.objects.create(
            name="dream",
            daily_at=dt.time(3, 0),
            delay_seconds=86400,
            script="src/teatree/loops/dream/loop.py",
            enabled=True,
            override_reason="test override",
        )
        counts = timer_reconciler.ensure_loop_timers()
        assert counts["added"] == 0
        assert timer_chains.pending_loop_timers("dream") == []


def _claim_and_fire(name: str) -> dict:
    """Claim the loop's queued timer the way a worker does, then run its body.

    A worker flips the READY row to RUNNING before invoking the task, so the body's
    step-1 self-dedup sees only its own RUNNING row; firing against the untouched
    READY row instead would dedup and never reach the tick.
    """
    [timer] = timer_chains.pending_loop_timers(name)
    DBTaskResult.objects.filter(id=timer.id).update(status=TaskResultStatus.RUNNING, started_at=timezone.now())
    context = types.SimpleNamespace(task_result=types.SimpleNamespace(id=timer.id))
    return timer_chains.loop_timer.func(context, name)


@django.test.override_settings(USE_TZ=True, TASKS=_DB_TASKS)
class TestReconcilerHonoursTheAdmissionVerdict(django.test.TestCase):
    """The chain is built from the admission verdict, not the raw ``Loop.enabled`` column (#4185).

    ``Loop.enabled`` is the MANUAL-override layer of that verdict (hold > manual >
    preset), empty on a fleet nobody has intervened on, so the preset decides the loop
    and the column answers about none of them. Building the chain from it alone left eight
    preset-admitted loops with no timer row of any status, ever — and because
    ``ensure_loop_timers`` PRUNES every timer outside the set it builds, a chain that
    did exist was actively removed rather than merely never created.
    """

    def setUp(self) -> None:
        Loop.objects.all().delete()
        DBTaskResult.objects.all().delete()
        Loop.objects.create(
            name="inbox",
            script="src/teatree/loops/inbox/loop.py",
            delay_seconds=60,
            last_run_at=None,
        )
        Mode.objects.create(name=_PRESET, entries={"inbox": True})
        ModeOverride.objects.set_override(_PRESET, reason="test override")

    def test_a_preset_admitted_loop_gets_a_head_and_ticks(self) -> None:
        assert timer_reconciler.ensure_loop_timers()["added"] == 1
        # A row in the results table, not a name the reconciler returned: a stub that
        # merely reported the loop as chained would be undone by the prune pass below it.
        [head] = timer_chains.pending_loop_timers("inbox")
        assert head.status == TaskResultStatus.READY

        ticked: list[str] = []

        def _tick(name: str, *, deadline: float) -> dict[str, object]:
            ticked.append(name)
            Loop.objects.mark_run(name, timezone.now())
            return {"timed_out": False, "returncode": 0}

        with mock.patch.object(timer_chains, "run_deadlined_tick", _tick):
            result = _claim_and_fire("inbox")

        assert result["action"] == "ticked"
        assert ticked == ["inbox"]
        assert len(timer_chains.pending_loop_timers("inbox")) == 1  # the successor carries the chain

    def test_does_not_prune_a_preset_admitted_loops_timer(self) -> None:
        timer_chains.enqueue_loop_timer("inbox", run_after=timezone.now())
        counts = timer_reconciler.ensure_loop_timers()
        assert counts["pruned"] == 0
        assert len(timer_chains.pending_loop_timers("inbox")) == 1

    def test_prunes_a_preset_masked_off_loops_timer(self) -> None:
        # The deliberate inverse: a loop the preset masks off loses its chain rather than
        # idle-polling at the cadence floor for every skipped fire.
        Mode.objects.filter(name=_PRESET).update(entries={"inbox": False})
        timer_chains.enqueue_loop_timer("inbox", run_after=timezone.now())
        assert timer_reconciler.ensure_loop_timers()["pruned"] == 1
        assert timer_chains.pending_loop_timers("inbox") == []

    def test_prunes_a_held_loops_timer_and_re_heads_on_resume(self) -> None:
        LoopState.objects.pause("inbox")
        timer_chains.enqueue_loop_timer("inbox", run_after=timezone.now())
        assert timer_reconciler.ensure_loop_timers()["pruned"] == 1
        assert timer_chains.pending_loop_timers("inbox") == []

        LoopState.objects.resume("inbox")
        assert timer_reconciler.ensure_loop_timers()["added"] == 1
        assert len(timer_chains.pending_loop_timers("inbox")) == 1


@django.test.override_settings(USE_TZ=True, TIME_ZONE="UTC", TASKS=_DB_TASKS)
class TestAScheduleSlotGetsAHeadAndTicks(ModeWithoutOverrideMixin):
    """#4185 AC1 in the configuration a source-``override`` test cannot reach (#4196).

    A schedule slot rather than a manual override: membership used to resolve the mask
    through a layer that stops before the default mode, so the chain the reconciler built
    disagreed with the tick. The timer row alone is not the acceptance criterion — the
    fire that row carries has to reach the tick.
    """

    def setUp(self) -> None:
        super().setUp()
        self.use_l0_default_mode()

    def test_the_head_exists_and_its_fire_ticks_the_loop(self) -> None:
        assert timer_reconciler.ensure_loop_timers()["added"] == 1
        [head] = timer_chains.pending_loop_timers(LOOP)
        assert head.status == TaskResultStatus.READY

        ticked: list[str] = []

        def _tick(name: str, *, deadline: float) -> dict[str, object]:
            ticked.append(name)
            Loop.objects.mark_run(name, timezone.now())
            return {"timed_out": False, "returncode": 0}

        with mock.patch.object(timer_chains, "run_deadlined_tick", _tick):
            result = _claim_and_fire(LOOP)

        assert result["action"] == "ticked"
        assert ticked == [LOOP]
        assert len(timer_chains.pending_loop_timers(LOOP)) == 1  # the successor carries the chain

    def test_an_existing_head_is_not_pruned(self) -> None:
        # The regression direction that stops two working loops: ``ensure_loop_timers``
        # DELETES the timers of a non-member, so a membership set narrower than the tick's
        # takes away chains that already existed.
        DBTaskResult.objects.all().delete()
        timer_chains.enqueue_loop_timer(LOOP, run_after=timezone.now())
        assert timer_reconciler.ensure_loop_timers()["pruned"] == 0
        assert len(timer_chains.pending_loop_timers(LOOP)) == 1


@django.test.override_settings(USE_TZ=True, TASKS=_DB_TASKS)
class TestMaintenanceChains(django.test.TestCase):
    def setUp(self) -> None:
        DBTaskResult.objects.all().delete()

    def test_seeds_reconcile_prune_and_expiry_heads_once(self) -> None:
        timer_reconciler.ensure_maintenance_chains()
        assert DBTaskResult.objects.filter(task_path=timer_reconciler.reconcile_timers.module_path).count() == 1
        assert DBTaskResult.objects.filter(task_path=timer_reconciler.prune_task_results.module_path).count() == 1
        assert DBTaskResult.objects.filter(task_path=timer_reconciler.expire_stale_jobs.module_path).count() == 1
        # #10: the headless-queue drain chain is seeded too (it had no other home).
        assert DBTaskResult.objects.filter(task_path=timer_reconciler.drain_chain.module_path).count() == 1
        assert DBTaskResult.objects.filter(task_path=timer_reconciler.run_slack_answer.module_path).count() == 1
        # The off-live-tick driver: without it directive_loop / dream / outer_loop have
        # NO driver at all — the live fan-out excludes them and no cron exists.
        assert (
            DBTaskResult.objects.filter(task_path=off_live_tick_driver.drive_off_live_tick_loops.module_path).count()
            == 1
        )
        # Idempotent: a second call adds no duplicates.
        timer_reconciler.ensure_maintenance_chains()
        assert DBTaskResult.objects.filter(task_path=timer_reconciler.reconcile_timers.module_path).count() == 1
        assert DBTaskResult.objects.filter(task_path=timer_reconciler.expire_stale_jobs.module_path).count() == 1
        assert DBTaskResult.objects.filter(task_path=timer_reconciler.drain_chain.module_path).count() == 1
        assert DBTaskResult.objects.filter(task_path=timer_reconciler.run_slack_answer.module_path).count() == 1
        assert (
            DBTaskResult.objects.filter(task_path=off_live_tick_driver.drive_off_live_tick_loops.module_path).count()
            == 1
        )

    def test_seeds_the_self_improve_chain(self) -> None:
        # The third reactive slot: drain-queue and slack-answer already ran as worker
        # chains, so only self-improve still forced a session to register a `/loop`.
        timer_reconciler.ensure_maintenance_chains()
        assert DBTaskResult.objects.filter(task_path=timer_reconciler.run_self_improve.module_path).count() == 1

        timer_reconciler.ensure_maintenance_chains()
        assert DBTaskResult.objects.filter(task_path=timer_reconciler.run_self_improve.module_path).count() == 1

    def test_run_self_improve_reschedules_itself(self) -> None:
        result = timer_reconciler.run_self_improve.func()
        assert "deduped" not in result
        pending = DBTaskResult.objects.filter(
            task_path=timer_reconciler.run_self_improve.module_path, status=TaskResultStatus.READY
        )
        assert pending.count() == 1

    def test_run_self_improve_self_dedups(self) -> None:
        timer_reconciler.run_self_improve.using(run_after=timezone.now()).enqueue()
        result = timer_reconciler.run_self_improve.func()
        assert result == {"deduped": 1}

    def test_run_self_improve_releases_its_lease(self) -> None:
        from teatree.core.models import LoopLease  # noqa: PLC0415 — deferred: ORM import needs the app registry

        self.enterContext(worker_owns_t3_master())
        timer_reconciler.run_self_improve.func()

        assert LoopLease.objects.acquire(timer_reconciler.SELF_IMPROVE_LEASE, owner="owner-session")

    def test_run_self_improve_skips_when_lease_held(self) -> None:
        from teatree.core.models import LoopLease  # noqa: PLC0415 — deferred: ORM import needs the app registry

        self.enterContext(worker_owns_t3_master())
        LoopLease.objects.acquire(timer_reconciler.SELF_IMPROVE_LEASE, owner="owner-session")

        assert timer_reconciler.run_self_improve.func() == {"skipped": 1}

    def test_run_self_improve_survives_a_body_fault(self) -> None:
        # Successor-first: a raising body must never orphan the chain.
        with mock.patch.object(timer_reconciler, "_run_self_improve_cycle_via_command", side_effect=RuntimeError("x")):
            assert timer_reconciler.run_self_improve.func() == {"error": 1}

        pending = DBTaskResult.objects.filter(
            task_path=timer_reconciler.run_self_improve.module_path, status=TaskResultStatus.READY
        )
        assert pending.count() == 1

    def test_drain_chain_reschedules_itself(self) -> None:
        result = timer_reconciler.drain_chain.func()
        assert "deduped" not in result
        pending = DBTaskResult.objects.filter(
            task_path=timer_reconciler.drain_chain.module_path, status=TaskResultStatus.READY
        )
        assert pending.count() == 1  # a successor drain chain is queued

    def test_drain_chain_self_dedups(self) -> None:
        timer_reconciler.drain_chain.using(run_after=timezone.now()).enqueue()
        result = timer_reconciler.drain_chain.func()
        assert result == {"deduped": 1}

    def test_expire_stale_jobs_reschedules_itself(self) -> None:
        result = timer_reconciler.expire_stale_jobs.func()
        assert "deduped" not in result
        pending = DBTaskResult.objects.filter(
            task_path=timer_reconciler.expire_stale_jobs.module_path, status=TaskResultStatus.READY
        )
        assert pending.count() == 1  # a successor expiry chain is queued

    def test_expire_stale_jobs_self_dedups(self) -> None:
        timer_reconciler.expire_stale_jobs.using(run_after=timezone.now()).enqueue()
        result = timer_reconciler.expire_stale_jobs.func()
        assert result == {"deduped": 1}

    def test_reconcile_timers_reschedules_itself(self) -> None:
        result = timer_reconciler.reconcile_timers.func()
        assert "deduped" not in result
        pending = DBTaskResult.objects.filter(
            task_path=timer_reconciler.reconcile_timers.module_path, status=TaskResultStatus.READY
        )
        assert pending.count() == 1  # a successor reconciler is queued

    def test_reconcile_timers_self_dedups(self) -> None:
        timer_reconciler.reconcile_timers.using(run_after=timezone.now()).enqueue()
        result = timer_reconciler.reconcile_timers.func()
        assert result == {"deduped": 1}

    def test_run_slack_answer_reschedules_itself(self) -> None:
        result = timer_reconciler.run_slack_answer.func()
        assert "deduped" not in result
        # Empty queue → the cycle runs and reports zero processed, then re-arms.
        assert result["processed"] == 0
        pending = DBTaskResult.objects.filter(
            task_path=timer_reconciler.run_slack_answer.module_path, status=TaskResultStatus.READY
        )
        assert pending.count() == 1  # a successor slack-answer chain is queued

    def test_run_slack_answer_self_dedups(self) -> None:
        timer_reconciler.run_slack_answer.using(run_after=timezone.now()).enqueue()
        result = timer_reconciler.run_slack_answer.func()
        assert result == {"deduped": 1}

    def test_run_slack_answer_releases_its_lease(self) -> None:
        from teatree.core.models import LoopLease  # noqa: PLC0415 — deferred: ORM import needs the app registry

        timer_reconciler.run_slack_answer.func()
        # The worker took-and-released the shared slot, so an owner session can claim it.
        assert LoopLease.objects.acquire(timer_reconciler.SLACK_ANSWER_LEASE, owner="owner-session")

    def test_run_slack_answer_skips_when_lease_held(self) -> None:
        from teatree.core.models import LoopLease  # noqa: PLC0415 — deferred: ORM import needs the app registry

        # An interactive owner session already holds the shared slot.
        assert LoopLease.objects.acquire(timer_reconciler.SLACK_ANSWER_LEASE, owner="owner-session")
        result = timer_reconciler.run_slack_answer.func()
        assert result == {"skipped_lease_held": 1}
        # Skipping the cycle must NOT release the owner's lease…
        assert not LoopLease.objects.acquire(timer_reconciler.SLACK_ANSWER_LEASE, owner="worker-other")
        # …but the chain is still re-armed so it keeps ticking.
        pending = DBTaskResult.objects.filter(
            task_path=timer_reconciler.run_slack_answer.module_path, status=TaskResultStatus.READY
        )
        assert pending.count() == 1

    def test_wake_slack_answer_enqueues_immediate_not_cadence_delayed(self) -> None:
        # The per-event drain trigger the receiver fires: an unscheduled enqueue
        # stores the run-now sentinel, so the worker's ~1s poll picks it up right
        # away — unlike run_slack_answer, which delays the next run by the cadence.
        timer_reconciler.wake_slack_answer.enqueue()
        ready = DBTaskResult.objects.filter(
            task_path=timer_reconciler.wake_slack_answer.module_path, status=TaskResultStatus.READY
        )
        assert ready.count() == 1
        assert ready.get().run_after == get_date_max()

    def test_wake_slack_answer_runs_cycle_and_does_not_reschedule(self) -> None:
        result = timer_reconciler.wake_slack_answer.func()
        assert "deduped" not in result
        # Empty queue → the cycle runs and reports zero processed.
        assert result["processed"] == 0
        # One-shot: unlike the cadence chain, a wake never re-arms itself.
        assert (
            DBTaskResult.objects.filter(
                task_path=timer_reconciler.wake_slack_answer.module_path, status=TaskResultStatus.READY
            ).count()
            == 0
        )

    def test_wake_slack_answer_self_dedups(self) -> None:
        timer_reconciler.wake_slack_answer.using(run_after=timezone.now()).enqueue()
        result = timer_reconciler.wake_slack_answer.func()
        assert result == {"deduped": 1}

    def test_wake_slack_answer_coalesces_behind_a_just_finished_wake(self) -> None:
        # #4707: a cycle takes ~2s, so events arriving slower than that found
        # nothing pending and bought a cycle each — 290 an hour at the peak.
        finished = timezone.now() - dt.timedelta(seconds=2)
        DBTaskResult.objects.create(
            task_path=timer_reconciler.wake_slack_answer.module_path,
            status=TaskResultStatus.SUCCESSFUL,
            args_kwargs={"args": [], "kwargs": {}},
            backend_name="default",
            queue_name=timer_chains.LOOPS_QUEUE,
            finished_at=finished,
        )

        result = timer_reconciler.wake_slack_answer.func()

        assert result == {"coalesced": 1}
        # Trailing edge: the burst's last event is still answered, one interval
        # late at worst, rather than waiting out the whole 5m cadence.
        ready = DBTaskResult.objects.filter(
            task_path=timer_reconciler.wake_slack_answer.module_path, status=TaskResultStatus.READY
        )
        assert ready.count() == 1
        expected = finished + dt.timedelta(seconds=timer_reconciler.WAKE_MIN_INTERVAL_SECONDS)
        assert ready.get().run_after == expected

    def test_wake_slack_answer_runs_once_the_interval_has_passed(self) -> None:
        # The control: the debounce must expire, or the wake path is dead.
        DBTaskResult.objects.create(
            task_path=timer_reconciler.wake_slack_answer.module_path,
            status=TaskResultStatus.SUCCESSFUL,
            args_kwargs={"args": [], "kwargs": {}},
            backend_name="default",
            queue_name=timer_chains.LOOPS_QUEUE,
            finished_at=timezone.now() - dt.timedelta(seconds=timer_reconciler.WAKE_MIN_INTERVAL_SECONDS + 1),
        )

        result = timer_reconciler.wake_slack_answer.func()

        assert result["processed"] == 0
        assert "coalesced" not in result

    def test_wake_slack_answer_runs_behind_a_wake_that_ran_no_cycle(self) -> None:
        # A coalesced wake's own finish must not debounce its re-armed successor,
        # or the chain re-arms every interval forever and never runs a cycle.
        for no_cycle in ({"coalesced": 1}, {"deduped": 1}):
            with self.subTest(no_cycle=no_cycle):
                DBTaskResult.objects.all().delete()
                DBTaskResult.objects.create(
                    task_path=timer_reconciler.wake_slack_answer.module_path,
                    status=TaskResultStatus.SUCCESSFUL,
                    args_kwargs={"args": [], "kwargs": {}},
                    backend_name="default",
                    queue_name=timer_chains.LOOPS_QUEUE,
                    finished_at=timezone.now() - dt.timedelta(seconds=1),
                    return_value=no_cycle,
                )

                result = timer_reconciler.wake_slack_answer.func()

                assert result["processed"] == 0

    def test_a_coalesced_wake_chain_still_runs_a_cycle(self) -> None:
        path = timer_reconciler.wake_slack_answer.module_path

        def finished(at: dt.datetime, result: dict[str, int]) -> None:
            DBTaskResult.objects.create(
                task_path=path,
                status=TaskResultStatus.SUCCESSFUL,
                args_kwargs={"args": [], "kwargs": {}},
                backend_name="default",
                queue_name=timer_chains.LOOPS_QUEUE,
                finished_at=at,
                return_value=result,
            )

        last_cycle = timezone.now()
        finished(last_cycle, {"processed": 0})
        now = last_cycle + dt.timedelta(seconds=2)
        results = []
        for _ in range(12):
            with mock.patch("django.utils.timezone.now", return_value=now):
                results.append(timer_reconciler.wake_slack_answer.func())
            finished(now, results[-1])
            successor = DBTaskResult.objects.filter(task_path=path, status=TaskResultStatus.READY).first()
            if successor is None:
                break
            now = successor.run_after
            successor.delete()

        assert "processed" in results[-1], results

    def test_wake_slack_answer_ignores_another_chains_recent_finish(self) -> None:
        # The window is keyed on the wake's own path; the cadence chain finishing
        # must not debounce an event-driven wake.
        DBTaskResult.objects.create(
            task_path=timer_reconciler.run_slack_answer.module_path,
            status=TaskResultStatus.SUCCESSFUL,
            args_kwargs={"args": [], "kwargs": {}},
            backend_name="default",
            queue_name=timer_chains.LOOPS_QUEUE,
            finished_at=timezone.now(),
        )

        result = timer_reconciler.wake_slack_answer.func()

        assert result["processed"] == 0
        assert "coalesced" not in result

    def test_wake_slack_answer_skips_when_lease_held(self) -> None:
        from teatree.core.models import LoopLease  # noqa: PLC0415 — deferred: ORM import needs the app registry

        # An interactive owner session (or the cadence chain) already holds the slot.
        assert LoopLease.objects.acquire(timer_reconciler.SLACK_ANSWER_LEASE, owner="owner-session")
        result = timer_reconciler.wake_slack_answer.func()
        assert result == {"skipped_lease_held": 1}
        # Skipping must NOT release the holder's lease.
        assert not LoopLease.objects.acquire(timer_reconciler.SLACK_ANSWER_LEASE, owner="worker-other")

    def test_wake_slack_answer_releases_its_lease(self) -> None:
        from teatree.core.models import LoopLease  # noqa: PLC0415 — deferred: ORM import needs the app registry

        timer_reconciler.wake_slack_answer.func()
        # The wake took-and-released the shared slot, so an owner session can claim it.
        assert LoopLease.objects.acquire(timer_reconciler.SLACK_ANSWER_LEASE, owner="owner-session")

    def test_prune_removes_only_old_finished_results(self) -> None:
        old = DBTaskResult.objects.create(
            task_path="x.old",
            args_kwargs={"args": [], "kwargs": {}},
            backend_name="default",
            status=TaskResultStatus.SUCCESSFUL,
            run_after=get_date_max(),
            finished_at=timezone.now() - dt.timedelta(days=2),
        )
        recent = DBTaskResult.objects.create(
            task_path="x.recent",
            args_kwargs={"args": [], "kwargs": {}},
            backend_name="default",
            status=TaskResultStatus.SUCCESSFUL,
            run_after=get_date_max(),
            finished_at=timezone.now(),
        )
        result = timer_reconciler.prune_task_results.func()
        assert result["pruned"] == 1
        assert not DBTaskResult.objects.filter(id=old.id).exists()
        assert DBTaskResult.objects.filter(id=recent.id).exists()

    def test_prune_honours_the_configured_window(self) -> None:
        from teatree.core.models.config_setting import ConfigSetting  # noqa: PLC0415 — deferred: ORM/app-registry

        ConfigSetting.objects.set_value("task_result_retention_days", 3)
        row = DBTaskResult.objects.create(
            task_path="x.old",
            args_kwargs={"args": [], "kwargs": {}},
            backend_name="default",
            status=TaskResultStatus.SUCCESSFUL,
            run_after=get_date_max(),
            finished_at=timezone.now() - dt.timedelta(days=2),
        )
        assert timer_reconciler.prune_task_results.func() == {"pruned": 0}
        assert DBTaskResult.objects.filter(id=row.id).exists()

    def test_prune_reaches_the_loops_queue(self) -> None:
        """The chains ride ``loops``; the library's own default would skip that queue."""
        row = DBTaskResult.objects.create(
            task_path="x.old",
            args_kwargs={"args": [], "kwargs": {}},
            backend_name="default",
            queue_name="loops",
            status=TaskResultStatus.SUCCESSFUL,
            run_after=get_date_max(),
            finished_at=timezone.now() - dt.timedelta(days=2),
        )
        assert timer_reconciler.prune_task_results.func() == {"pruned": 1}
        assert not DBTaskResult.objects.filter(id=row.id).exists()


@django.test.override_settings(USE_TZ=True, TASKS=_DB_TASKS)
class TestReapStuckHeadlessRuns(django.test.TestCase):
    """#10: a ``execute_task`` left RUNNING by a dead worker is reaped + re-enqueued."""

    def setUp(self) -> None:
        DBTaskResult.objects.all().delete()

    def _claimed_task(
        self,
        *,
        lease_delta_seconds: int,
        status: str = Task.Status.CLAIMED,
        owner_pid: int | None = None,
    ) -> Task:
        ticket = Ticket.objects.create(overlay="test", role=Ticket.Role.AUTHOR)
        session = Session.objects.create(ticket=ticket, overlay="test")
        return Task.objects.create(
            ticket=ticket,
            session=session,
            phase="architectural_review",
            status=status,
            claimed_by="task-worker",
            heartbeat_at=timezone.now(),
            lease_expires_at=timezone.now() + dt.timedelta(seconds=lease_delta_seconds),
            owner_pid=owner_pid,
            owner_pid_namespace=_READER_NS if owner_pid is not None else "",
        )

    def _running_headless_row(self, task: Task, *, age_seconds: int) -> DBTaskResult:
        result = execute_task.enqueue(task.pk, task.phase)
        DBTaskResult.objects.filter(id=result.id).update(
            status=TaskResultStatus.RUNNING,
            started_at=timezone.now() - dt.timedelta(seconds=age_seconds),
        )
        return DBTaskResult.objects.get(id=result.id)

    def _dead_age(self) -> int:
        return timer_reconciler.HEADLESS_LEASE_SECONDS + timer_reconciler.STUCK_GRACE_SECONDS + 60

    def test_dead_run_is_failed_and_task_reenqueued(self) -> None:
        task = self._claimed_task(lease_delta_seconds=-120)  # heartbeat stopped: lease lapsed
        row = self._running_headless_row(task, age_seconds=self._dead_age())

        counts = reap_stuck_runs()

        assert counts == {"failed": 1, "reenqueued": 1}
        row.refresh_from_db()
        assert row.status == TaskResultStatus.FAILED
        ready = DBTaskResult.objects.filter(task_path=execute_task.module_path, status=TaskResultStatus.READY)
        assert ready.count() == 1, "the non-terminal task must be re-enqueued for a fresh run"

    def test_live_run_with_fresh_lease_is_not_reaped(self) -> None:
        # Old RUNNING row, but the heartbeat is still renewing the lease → alive.
        task = self._claimed_task(lease_delta_seconds=+200)
        row = self._running_headless_row(task, age_seconds=self._dead_age())

        counts = reap_stuck_runs()

        assert counts == {"failed": 0, "reenqueued": 0}
        row.refresh_from_db()
        assert row.status == TaskResultStatus.RUNNING

    def test_recently_started_run_within_floor_is_not_reaped(self) -> None:
        # A just-claimed run whose lease is briefly unset is protected by the floor.
        task = self._claimed_task(lease_delta_seconds=-10)
        row = self._running_headless_row(task, age_seconds=30)

        counts = reap_stuck_runs()

        assert counts == {"failed": 0, "reenqueued": 0}
        row.refresh_from_db()
        assert row.status == TaskResultStatus.RUNNING

    def test_dead_run_with_terminal_task_is_failed_but_not_reenqueued(self) -> None:
        task = self._claimed_task(lease_delta_seconds=-120, status=Task.Status.COMPLETED)
        self._running_headless_row(task, age_seconds=self._dead_age())

        counts = reap_stuck_runs()

        assert counts == {"failed": 1, "reenqueued": 0}
        assert not DBTaskResult.objects.filter(
            task_path=execute_task.module_path, status=TaskResultStatus.READY
        ).exists()

    def test_running_row_with_no_started_at_is_not_reaped(self) -> None:
        # A row claimed-but-not-yet-started has no started_at → never a dead run.
        task = self._claimed_task(lease_delta_seconds=-120)
        result = execute_task.enqueue(task.pk, task.phase)
        DBTaskResult.objects.filter(id=result.id).update(status=TaskResultStatus.RUNNING, started_at=None)

        counts = reap_stuck_runs()

        assert counts == {"failed": 0, "reenqueued": 0}

    def test_orphaned_run_whose_task_is_gone_is_failed_not_reenqueued(self) -> None:
        # The Task row vanished (cascade delete) but its RUNNING DBTaskResult
        # lingers — an orphan: fail it, nothing to re-enqueue.
        task = self._claimed_task(lease_delta_seconds=-120)
        row = self._running_headless_row(task, age_seconds=self._dead_age())
        task.delete()

        counts = reap_stuck_runs()

        assert counts == {"failed": 1, "reenqueued": 0}
        row.refresh_from_db()
        assert row.status == TaskResultStatus.FAILED

    def test_a_stalled_but_still_driving_run_is_neither_failed_nor_duplicated(self) -> None:
        """#4164: ``set_failed`` marks the row only — it does not kill the process.

        So re-enqueuing here puts a SECOND agent on the same worktree while the first is
        still executing, and the duplicate wins the claim because the CAS reads an expired
        lease as claimable.
        """
        task = self._claimed_task(lease_delta_seconds=-120, owner_pid=os.getpid())
        row = self._running_headless_row(task, age_seconds=self._dead_age())

        with pinned_reader_namespace(), driving(task.pk):
            counts = reap_stuck_runs()

        assert counts == {"failed": 0, "reenqueued": 0}
        row.refresh_from_db()
        assert row.status == TaskResultStatus.RUNNING
        assert not DBTaskResult.objects.filter(
            task_path=execute_task.module_path, status=TaskResultStatus.READY
        ).exists()

    def test_a_run_nothing_is_driving_is_still_reaped(self) -> None:
        """A crashed job leaves this worker alive; the stranded row must still recover."""
        task = self._claimed_task(lease_delta_seconds=-120, owner_pid=os.getpid())
        self._running_headless_row(task, age_seconds=self._dead_age())

        counts = reap_stuck_runs()

        assert counts == {"failed": 1, "reenqueued": 1}

    def test_headless_run_with_no_args_is_skipped(self) -> None:
        # A malformed row carrying no args resolves to no task id and is left alone.
        DBTaskResult.objects.create(
            task_path=execute_task.module_path,
            args_kwargs={"args": [], "kwargs": {}},
            backend_name="default",
            status=TaskResultStatus.RUNNING,
            started_at=timezone.now() - dt.timedelta(seconds=self._dead_age()),
            run_after=get_date_max(),
        )

        counts = reap_stuck_runs()

        assert counts == {"failed": 0, "reenqueued": 0}

    def test_a_stopped_fleet_holds_a_dead_run_and_lifting_it_re_dispatches_once(self) -> None:
        task = self._claimed_task(lease_delta_seconds=-120)
        row = self._running_headless_row(task, age_seconds=self._dead_age())
        ModeOverride.objects.set_override("off", reason="test: the owner stopped the fleet")

        self._fire_drain_chain()

        assert not DBTaskResult.objects.filter(**_READY_HEADLESS_JOB).exists()

        ModeOverride.objects.all().delete()
        self._fire_drain_chain()
        self._fire_drain_chain()

        row.refresh_from_db()
        assert row.status == TaskResultStatus.FAILED
        assert DBTaskResult.objects.filter(**_READY_HEADLESS_JOB).count() == 1

    def test_an_unreadable_admission_verdict_holds_the_dead_run(self) -> None:
        task = self._claimed_task(lease_delta_seconds=-120)
        self._running_headless_row(task, age_seconds=self._dead_age())

        with mock.patch("teatree.core.managers.claim_admission_block_reason", side_effect=RuntimeError("locked")):
            counts = reap_stuck_runs()

        assert counts == {"failed": 0, "reenqueued": 0}
        assert not DBTaskResult.objects.filter(**_READY_HEADLESS_JOB).exists()

    def test_two_reapers_released_from_off_enqueue_one_replacement(self) -> None:
        task = self._claimed_task(lease_delta_seconds=-120)
        self._running_headless_row(task, age_seconds=self._dead_age())
        ModeOverride.objects.set_override("off", reason="test: the owner stopped the fleet")
        self._fire_drain_chain()
        ModeOverride.objects.all().delete()
        is_dead = timer_reconciler._headless_run_is_dead
        rival_ran: list[bool] = []

        def a_rival_reaper_scans_the_same_row(*args: object, **kwargs: object) -> bool:
            if not rival_ran:
                rival_ran.append(True)
                reap_stuck_runs()
            return is_dead(*args, **kwargs)

        with mock.patch.object(
            timer_reconciler, "_headless_run_is_dead", side_effect=a_rival_reaper_scans_the_same_row
        ):
            reap_stuck_runs()

        assert DBTaskResult.objects.filter(**_READY_HEADLESS_JOB).count() == 1

    def test_a_stop_landing_after_the_scan_holds_the_dead_run(self) -> None:
        task = self._claimed_task(lease_delta_seconds=-120)
        row = self._running_headless_row(task, age_seconds=self._dead_age())
        is_dead = timer_reconciler._headless_run_is_dead

        def the_fleet_stops_after_the_scan(*args: object, **kwargs: object) -> bool:
            ModeOverride.objects.set_override("off", reason="test: the owner stopped the fleet mid-reap")
            return is_dead(*args, **kwargs)

        with mock.patch.object(timer_reconciler, "_headless_run_is_dead", side_effect=the_fleet_stops_after_the_scan):
            counts = reap_stuck_runs()

        row.refresh_from_db()
        assert counts == {"failed": 0, "reenqueued": 0}
        assert row.status == TaskResultStatus.RUNNING
        assert not DBTaskResult.objects.filter(**_READY_HEADLESS_JOB).exists()

    def _fire_drain_chain(self) -> None:
        DBTaskResult.objects.filter(task_path=timer_reconciler.drain_chain.module_path).delete()
        timer_reconciler.drain_chain.func()


@django.test.override_settings(USE_TZ=True, TASKS=_DB_TASKS)
class TestDrainChainFreesEveryStrandedPath(django.test.TestCase):
    """The maintenance pass must leave no RUNNING row of ANY task path wedged forever.

    Measured on the running box: 33 RUNNING rows surviving indefinitely, the oldest 26
    days. Every reaper is keyed to one path or one status — ``ensure_loop_timers``
    repairs only an ADMITTED loop's ``loop_timer``, ``reap_stuck_runs`` filters on
    ``execute_task``, ``expire_stale_jobs`` retires only READY, ``prune_task_results``
    skips READY and RUNNING alike — so a row from any other path is unreachable by all
    of them. ``STRANDED_JOB_GRACE_SECONDS``'s own docstring names the gap ("every reaper
    is per-task-path"); two readers already judge RUNNING rows against it, and nothing
    ever writes the verdict back.
    """

    def setUp(self) -> None:
        DBTaskResult.objects.all().delete()
        pid_file = Path(self.enterContext(tempfile.TemporaryDirectory())) / "worker.pid"
        pid_file.write_text(f"{os.getpid()}\n", encoding="utf-8")
        self.enterContext(mock.patch("teatree.utils.singleton.default_pid_path", return_value=pid_file))

    def _stranded(self, *, task_path: str, queue_name: str, age_days: int) -> DBTaskResult:
        result = execute_task.enqueue(1, "coding")
        DBTaskResult.objects.filter(id=result.id).update(
            task_path=task_path,
            queue_name=queue_name,
            status=TaskResultStatus.RUNNING,
            started_at=timezone.now() - dt.timedelta(days=age_days),
            worker_ids=[f"worker-{_REPLACED_WORKER_PID}-0-{queue_name}"],
        )
        return DBTaskResult.objects.get(id=result.id)

    def test_a_days_old_off_live_tick_drive_is_freed(self) -> None:
        row = self._stranded(
            task_path=off_live_tick_driver.drive_off_live_tick_loops.module_path,
            queue_name=timer_chains.LOOPS_QUEUE,
            age_days=22,
        )

        timer_reconciler.drain_chain.func()

        row.refresh_from_db()
        assert row.status == TaskResultStatus.FAILED, "a 22-day-old RUNNING drive is a corpse, not in flight"

    def test_a_retired_loops_timer_row_is_freed(self) -> None:
        # `pane_reaper` was dropped by migration 0033; ensure_loop_timers reaps a RUNNING
        # timer only for an ADMITTED loop, and its comment's "a RUNNING one dies on its
        # own next fire" never happens once the chain is gone.
        result = timer_chains.loop_timer.enqueue("pane_reaper")
        DBTaskResult.objects.filter(id=result.id).update(
            status=TaskResultStatus.RUNNING,
            started_at=timezone.now() - dt.timedelta(days=26),
            worker_ids=[f"worker-{_REPLACED_WORKER_PID}-0-loops"],
        )

        timer_reconciler.drain_chain.func()

        assert DBTaskResult.objects.get(id=result.id).status == TaskResultStatus.FAILED

    def test_a_task_path_that_no_longer_exists_is_freed(self) -> None:
        row = self._stranded(
            task_path="teatree.loops.timer_reconciler.drain_headless_chain",
            queue_name=timer_chains.LOOPS_QUEUE,
            age_days=7,
        )

        timer_reconciler.drain_chain.func()

        row.refresh_from_db()
        assert row.status == TaskResultStatus.FAILED

    def test_a_row_the_live_worker_still_carries_survives_the_pass(self) -> None:
        # Age bounds a plausible runtime; the carrier stamped at claim time is the fact.
        # A 22-day-old row this very process claimed is in flight, not a corpse.
        result = execute_task.enqueue(1, "coding")
        DBTaskResult.objects.filter(id=result.id).update(
            task_path=off_live_tick_driver.drive_off_live_tick_loops.module_path,
            queue_name=timer_chains.LOOPS_QUEUE,
            status=TaskResultStatus.RUNNING,
            started_at=timezone.now() - dt.timedelta(days=22),
            worker_ids=[f"worker-{os.getpid()}-0-loops"],
        )

        timer_reconciler.drain_chain.func()

        assert DBTaskResult.objects.get(id=result.id).status == TaskResultStatus.RUNNING

    def test_a_stranded_teardown_stops_suppressing_its_own_retry(self) -> None:
        # TeardownDispatch.outstanding_for reads a RUNNING teardown past the grace as
        # stranded already; leaving the row RUNNING keeps the queue disagreeing with the
        # only reader that acts on it.
        row = self._stranded(task_path="teatree.core.tasks.execute_teardown", queue_name="default", age_days=24)

        timer_reconciler.drain_chain.func()

        row.refresh_from_db()
        assert row.status == TaskResultStatus.FAILED
