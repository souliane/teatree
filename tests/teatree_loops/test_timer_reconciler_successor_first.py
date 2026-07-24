"""The five maintenance chains reschedule successor-FIRST (F6).

Each chain queues its next fire BEFORE running its body, in a try that
records-but-never-propagates. So a transient body exception (a DB lock, a connector
blip) can no longer orphan the chain — including ``reconcile_timers``, the chain that
repairs every other one. Before the fix the successor enqueue ran LAST, so a body
exception killed the chain until a worker restart.
"""

from unittest.mock import patch

import django.test
from django_tasks.base import TaskResultStatus
from django_tasks_db.models import DBTaskResult

from teatree.loops import timer_reconciler

_DB_TASKS = {"default": {"BACKEND": "django_tasks_db.DatabaseBackend", "QUEUES": ["default", "loops"]}}


def _boom(*_args: object, **_kwargs: object) -> object:
    msg = "transient body fault"
    raise RuntimeError(msg)


def _ready_successors(module_path: str) -> int:
    return DBTaskResult.objects.filter(task_path=module_path, status=TaskResultStatus.READY).count()


@django.test.override_settings(USE_TZ=True, TASKS=_DB_TASKS)
class TestSuccessorScheduledBeforeBody(django.test.TestCase):
    def setUp(self) -> None:
        DBTaskResult.objects.all().delete()

    def test_reconcile_timers_survives_body_exception_and_keeps_the_chain(self) -> None:
        with patch.object(timer_reconciler, "ensure_loop_timers", _boom):
            result = timer_reconciler.reconcile_timers.func()
        assert result == {"error": 1}  # recorded, not propagated
        assert _ready_successors(timer_reconciler.reconcile_timers.module_path) == 1

    def test_expire_stale_jobs_survives_body_exception_and_keeps_the_chain(self) -> None:
        with patch("teatree.loop.queue_drain.expire_stale_default_jobs", _boom):
            result = timer_reconciler.expire_stale_jobs.func()
        assert result == {"error": 1}
        assert _ready_successors(timer_reconciler.expire_stale_jobs.module_path) == 1

    def test_drain_headless_chain_survives_body_exception_and_keeps_the_chain(self) -> None:
        with patch.object(timer_reconciler, "reap_stuck_headless_runs", _boom):
            result = timer_reconciler.drain_headless_chain.func()
        assert result == {"error": 1}
        assert _ready_successors(timer_reconciler.drain_headless_chain.module_path) == 1

    def test_run_slack_answer_survives_body_exception_and_keeps_the_chain(self) -> None:
        with patch.object(timer_reconciler, "_run_slack_answer_cycle_under_lease", _boom):
            result = timer_reconciler.run_slack_answer.func()
        assert result == {"error": 1}
        assert _ready_successors(timer_reconciler.run_slack_answer.module_path) == 1

    def test_prune_task_results_survives_body_exception_and_keeps_the_chain(self) -> None:
        real_filter = DBTaskResult.objects.filter

        def _filter(*args: object, **kwargs: object) -> object:
            # The delete body filters on ``status__in``; ``_pending_for_path`` filters on
            # ``status=`` — raise only for the body so the pre-body dedup + reschedule run.
            if "status__in" in kwargs:
                _boom()
            return real_filter(*args, **kwargs)

        with patch.object(DBTaskResult.objects, "filter", _filter):
            result = timer_reconciler.prune_task_results.func()
        assert result == {"error": 1}
        assert _ready_successors(timer_reconciler.prune_task_results.module_path) == 1
