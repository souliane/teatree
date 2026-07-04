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

from teatree.core.models import Loop
from teatree.loops import timer_chains, timer_reconciler

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

    def test_seeds_reconcile_and_prune_heads_once(self) -> None:
        timer_reconciler.ensure_maintenance_chains()
        assert DBTaskResult.objects.filter(task_path=timer_reconciler.reconcile_timers.module_path).count() == 1
        assert DBTaskResult.objects.filter(task_path=timer_reconciler.prune_task_results.module_path).count() == 1
        # Idempotent: a second call adds no duplicates.
        timer_reconciler.ensure_maintenance_chains()
        assert DBTaskResult.objects.filter(task_path=timer_reconciler.reconcile_timers.module_path).count() == 1

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
