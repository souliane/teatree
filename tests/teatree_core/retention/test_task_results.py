"""``DBTaskResult`` retention — delegation to the library's own shipped prune (#3871).

``django-tasks-db`` ships ``manage.py prune_db_task_results``, so teatree owns no
second definition of "which result rows are disposable". These tests pin the two
properties that keeps honest: the count the dry run reports is the count the delete
removes (parity, so a library predicate change surfaces as a red test rather than a
lying report), and a READY/RUNNING row is never touched.
"""

import datetime as dt

from django.test import TestCase, override_settings
from django.utils import timezone
from django_tasks.base import TaskResultStatus
from django_tasks_db.models import DBTaskResult

from teatree.core.retention.task_results import (
    prunable_task_results,
    prune_finished_task_results,
    task_results_are_stored_in_the_db,
)

_OLD = timezone.now() - dt.timedelta(days=30)
_RECENT = timezone.now() - dt.timedelta(hours=1)


def _result(
    *,
    status: str = TaskResultStatus.SUCCESSFUL,
    finished_at: dt.datetime | None = _OLD,
    queue_name: str = "default",
    backend_name: str = "default",
) -> DBTaskResult:
    return DBTaskResult.objects.create(
        status=status,
        finished_at=finished_at,
        args_kwargs={"args": [], "kwargs": {}},
        task_path="teatree.loops.timer_chains.loop_timer",
        queue_name=queue_name,
        backend_name=backend_name,
        run_after=timezone.now(),
        exception_class_path="",
        traceback="",
    )


class TaskResultBackendProbeTestCase(TestCase):
    def test_a_non_database_backend_is_reported_as_having_no_result_table(self) -> None:
        assert task_results_are_stored_in_the_db() is False

    @override_settings(TASKS={"default": {"BACKEND": "django_tasks_db.DatabaseBackend"}})
    def test_the_database_backend_is_reported_as_storing_results(self) -> None:
        assert task_results_are_stored_in_the_db() is True


class PrunableTaskResultsTestCase(TestCase):
    def test_old_finished_result_is_prunable(self) -> None:
        row = _result()
        assert list(prunable_task_results(_RECENT).values_list("id", flat=True)) == [row.id]

    def test_old_failed_result_is_prunable(self) -> None:
        row = _result(status=TaskResultStatus.FAILED)
        assert list(prunable_task_results(_RECENT).values_list("id", flat=True)) == [row.id]

    def test_ready_result_is_never_prunable(self) -> None:
        _result(status=TaskResultStatus.READY, finished_at=None)
        assert prunable_task_results(_RECENT).count() == 0

    def test_running_result_is_never_prunable(self) -> None:
        _result(status=TaskResultStatus.RUNNING, finished_at=None)
        assert prunable_task_results(_RECENT).count() == 0

    def test_result_within_window_is_never_prunable(self) -> None:
        _result(finished_at=timezone.now())
        assert prunable_task_results(_RECENT).count() == 0

    def test_the_loops_queue_is_in_scope(self) -> None:
        """The chain rows ride ``loops``; a default-queue-only lane would never see them."""
        row = _result(queue_name="loops")
        assert list(prunable_task_results(_RECENT).values_list("id", flat=True)) == [row.id]


#: The library's prune command refuses any backend that is not a ``DatabaseBackend``,
#: so the delete tests must run under the production topology — the suite's default
#: ``DummyBackend`` would make them assert against a command that never ran.
_DATABASE_BACKEND = {
    "default": {"BACKEND": "django_tasks_db.DatabaseBackend", "QUEUES": ["default", "loops"]},
}


@override_settings(TASKS=_DATABASE_BACKEND)
class PruneFinishedTaskResultsTestCase(TestCase):
    def test_deletes_exactly_what_the_dry_run_counted(self) -> None:
        _result()
        _result(status=TaskResultStatus.FAILED, queue_name="loops")
        live = _result(status=TaskResultStatus.READY, finished_at=None)
        recent = _result(finished_at=timezone.now())

        planned = prunable_task_results(timezone.now() - dt.timedelta(days=1)).count()
        deleted = prune_finished_task_results(days=1)

        assert planned == 2
        assert deleted == planned
        assert set(DBTaskResult.objects.values_list("id", flat=True)) == {live.id, recent.id}

    def test_is_idempotent(self) -> None:
        _result()
        assert prune_finished_task_results(days=1) == 1
        assert prune_finished_task_results(days=1) == 0
