"""teatree.loops.timer_reconciler — the deterministic zero-token chain reconciler (#1796).

Integration-first against the real DB + ``django_tasks_db`` backend: the reconciler
adds a missing head, prunes a surplus timer, repairs a stranded RUNNING chain, and
deletes a disabled/unknown loop's timers — all without dispatching anything.
"""

import datetime as dt

import django.test
from django.utils import timezone
from django_tasks.base import TaskResultStatus
from django_tasks_db.models import DBTaskResult, get_date_max

from teatree.core.models import Loop, Session, Task, Ticket
from teatree.core.tasks import execute_headless_task
from teatree.loops import timer_chains, timer_reconciler
from teatree.loops.timer_reconciler import reap_stuck_headless_runs

_DB_TASKS = {"default": {"BACKEND": "django_tasks_db.DatabaseBackend", "QUEUES": ["default", "loops"]}}


@django.test.override_settings(USE_TZ=True, TASKS=_DB_TASKS)
class TestEnsureLoopTimers(django.test.TestCase):
    def setUp(self) -> None:
        Loop.objects.all().delete()
        DBTaskResult.objects.all().delete()

    def _enable(self, name: str = "inbox", **kwargs: object) -> Loop:
        defaults: dict[str, object] = {"delay_seconds": 60, "enabled": True, "last_run_at": None}
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

    def test_deletes_a_disabled_loops_timer(self) -> None:
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
        # ``dream`` is a registered off_live_tick loop — driven by its own cron, never a timer.
        Loop.objects.create(
            name="dream",
            daily_at=dt.time(3, 0),
            delay_seconds=86400,
            script="src/teatree/loops/dream/loop.py",
            enabled=True,
        )
        counts = timer_reconciler.ensure_loop_timers()
        assert counts["added"] == 0
        assert timer_chains.pending_loop_timers("dream") == []


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
        assert DBTaskResult.objects.filter(task_path=timer_reconciler.drain_headless_chain.module_path).count() == 1
        assert DBTaskResult.objects.filter(task_path=timer_reconciler.run_slack_answer.module_path).count() == 1
        # Idempotent: a second call adds no duplicates.
        timer_reconciler.ensure_maintenance_chains()
        assert DBTaskResult.objects.filter(task_path=timer_reconciler.reconcile_timers.module_path).count() == 1
        assert DBTaskResult.objects.filter(task_path=timer_reconciler.expire_stale_jobs.module_path).count() == 1
        assert DBTaskResult.objects.filter(task_path=timer_reconciler.drain_headless_chain.module_path).count() == 1
        assert DBTaskResult.objects.filter(task_path=timer_reconciler.run_slack_answer.module_path).count() == 1

    def test_drain_headless_chain_reschedules_itself(self) -> None:
        result = timer_reconciler.drain_headless_chain.func()
        assert "deduped" not in result
        pending = DBTaskResult.objects.filter(
            task_path=timer_reconciler.drain_headless_chain.module_path, status=TaskResultStatus.READY
        )
        assert pending.count() == 1  # a successor drain chain is queued

    def test_drain_headless_chain_self_dedups(self) -> None:
        timer_reconciler.drain_headless_chain.using(run_after=timezone.now()).enqueue()
        result = timer_reconciler.drain_headless_chain.func()
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


@django.test.override_settings(USE_TZ=True, TASKS=_DB_TASKS)
class TestReapStuckHeadlessRuns(django.test.TestCase):
    """#10: a ``execute_headless_task`` left RUNNING by a dead worker is reaped + re-enqueued."""

    def setUp(self) -> None:
        DBTaskResult.objects.all().delete()

    def _claimed_task(self, *, lease_delta_seconds: int, status: str = Task.Status.CLAIMED) -> Task:
        ticket = Ticket.objects.create(overlay="test", role=Ticket.Role.AUTHOR)
        session = Session.objects.create(ticket=ticket, overlay="test")
        return Task.objects.create(
            ticket=ticket,
            session=session,
            phase="architectural_review",
            status=status,
            claimed_by="headless-worker",
            lease_expires_at=timezone.now() + dt.timedelta(seconds=lease_delta_seconds),
        )

    def _running_headless_row(self, task: Task, *, age_seconds: int) -> DBTaskResult:
        result = execute_headless_task.enqueue(task.pk, task.phase)
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

        counts = reap_stuck_headless_runs()

        assert counts == {"failed": 1, "reenqueued": 1}
        row.refresh_from_db()
        assert row.status == TaskResultStatus.FAILED
        ready = DBTaskResult.objects.filter(task_path=execute_headless_task.module_path, status=TaskResultStatus.READY)
        assert ready.count() == 1, "the non-terminal task must be re-enqueued for a fresh run"

    def test_live_run_with_fresh_lease_is_not_reaped(self) -> None:
        # Old RUNNING row, but the heartbeat is still renewing the lease → alive.
        task = self._claimed_task(lease_delta_seconds=+200)
        row = self._running_headless_row(task, age_seconds=self._dead_age())

        counts = reap_stuck_headless_runs()

        assert counts == {"failed": 0, "reenqueued": 0}
        row.refresh_from_db()
        assert row.status == TaskResultStatus.RUNNING

    def test_recently_started_run_within_floor_is_not_reaped(self) -> None:
        # A just-claimed run whose lease is briefly unset is protected by the floor.
        task = self._claimed_task(lease_delta_seconds=-10)
        row = self._running_headless_row(task, age_seconds=30)

        counts = reap_stuck_headless_runs()

        assert counts == {"failed": 0, "reenqueued": 0}
        row.refresh_from_db()
        assert row.status == TaskResultStatus.RUNNING

    def test_dead_run_with_terminal_task_is_failed_but_not_reenqueued(self) -> None:
        task = self._claimed_task(lease_delta_seconds=-120, status=Task.Status.COMPLETED)
        self._running_headless_row(task, age_seconds=self._dead_age())

        counts = reap_stuck_headless_runs()

        assert counts == {"failed": 1, "reenqueued": 0}
        assert not DBTaskResult.objects.filter(
            task_path=execute_headless_task.module_path, status=TaskResultStatus.READY
        ).exists()

    def test_running_row_with_no_started_at_is_not_reaped(self) -> None:
        # A row claimed-but-not-yet-started has no started_at → never a dead run.
        task = self._claimed_task(lease_delta_seconds=-120)
        result = execute_headless_task.enqueue(task.pk, task.phase)
        DBTaskResult.objects.filter(id=result.id).update(status=TaskResultStatus.RUNNING, started_at=None)

        counts = reap_stuck_headless_runs()

        assert counts == {"failed": 0, "reenqueued": 0}

    def test_orphaned_run_whose_task_is_gone_is_failed_not_reenqueued(self) -> None:
        # The Task row vanished (cascade delete) but its RUNNING DBTaskResult
        # lingers — an orphan: fail it, nothing to re-enqueue.
        task = self._claimed_task(lease_delta_seconds=-120)
        row = self._running_headless_row(task, age_seconds=self._dead_age())
        task.delete()

        counts = reap_stuck_headless_runs()

        assert counts == {"failed": 1, "reenqueued": 0}
        row.refresh_from_db()
        assert row.status == TaskResultStatus.FAILED

    def test_headless_run_with_no_args_is_skipped(self) -> None:
        # A malformed row carrying no args resolves to no task id and is left alone.
        DBTaskResult.objects.create(
            task_path=execute_headless_task.module_path,
            args_kwargs={"args": [], "kwargs": {}},
            backend_name="default",
            status=TaskResultStatus.RUNNING,
            started_at=timezone.now() - dt.timedelta(seconds=self._dead_age()),
            run_after=get_date_max(),
        )

        counts = reap_stuck_headless_runs()

        assert counts == {"failed": 0, "reenqueued": 0}
