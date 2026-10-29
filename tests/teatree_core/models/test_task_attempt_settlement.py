from unittest import mock

import pytest
from django.db import OperationalError, transaction
from django.test import TestCase

from teatree.core.modelkit.task_failure_taxonomy import FailureKind
from teatree.core.models import NOOP_OVER_COMPLETED_MARKER, Session, Task, TaskAttempt, Ticket
from teatree.core.models.errors import LeaseLostError
from teatree.core.models.task_attempt_settlement import complete_with_strict_lease
from teatree.core.models.task_claim import _refuse_lost_lease


class TestTaskAttemptSettlement(TestCase):
    def test_late_failure_after_completion_records_interruption(self) -> None:
        ticket = Ticket.objects.create()
        task = Task.objects.create(ticket=ticket, session=Session.objects.create(ticket=ticket))
        task.claim(claimed_by="worker", claimed_by_session="run-1")
        Task.objects.filter(pk=task.pk).update(status=Task.Status.COMPLETED)

        attempt = task.complete_with_attempt(exit_code=1, error="run failed", result={"detail": "kept"})

        task.refresh_from_db()
        assert task.status == Task.Status.COMPLETED
        assert attempt.exit_code == 0
        assert attempt.error == ""
        assert "already completed" in str(attempt.result["summary"])
        assert NOOP_OVER_COMPLETED_MARKER in str(attempt.result["summary"])
        assert attempt.result["detail"] == "kept"
        assert task.attempts.count() == 1

    def test_late_failure_after_cancellation_keeps_run_error(self) -> None:
        ticket = Ticket.objects.create()
        task = Task.objects.create(ticket=ticket, session=Session.objects.create(ticket=ticket))
        task.claim(claimed_by="worker", claimed_by_session="run-1")
        Task.objects.filter(pk=task.pk).update(
            status=Task.Status.FAILED,
            failure_reason="cancelled: operator stopped the run",
            failure_kind=FailureKind.CANCELLED,
        )

        attempt = task.complete_with_attempt(exit_code=1, error="run interrupted")

        task.refresh_from_db()
        assert task.status == Task.Status.FAILED
        assert task.failure_reason == "cancelled: operator stopped the run"
        assert attempt.error == "run interrupted"
        assert "cancelled: operator stopped the run" in str(attempt.result["summary"])
        assert task.attempts.count() == 1

    def test_late_failure_after_reclaim_records_lease_loss(self) -> None:
        ticket = Ticket.objects.create()
        task = Task.objects.create(ticket=ticket, session=Session.objects.create(ticket=ticket))
        task.claim(claimed_by="worker-a", claimed_by_session="run-a")
        Task.objects.filter(pk=task.pk).update(claimed_by="worker-b")

        attempt = task.complete_with_attempt(exit_code=1, error="run failed")

        task.refresh_from_db()
        assert task.status == Task.Status.CLAIMED
        assert task.claimed_by == "worker-b"
        assert attempt.error.startswith("stuck_loop: lease lost for task")
        assert "run failed" in attempt.error
        assert task.attempts.count() == 1

    def test_lease_loss_after_precheck_reclassifies_one_record(self) -> None:
        ticket = Ticket.objects.create()
        task = Task.objects.create(ticket=ticket, session=Session.objects.create(ticket=ticket))
        task.claim(claimed_by="worker", claimed_by_session="run-1")

        def check_then_complete(checked: Task) -> None:
            _refuse_lost_lease(checked)
            Task.objects.filter(pk=task.pk).update(status=Task.Status.COMPLETED)

        with mock.patch(
            "teatree.core.models.task_attempt_settlement._refuse_lost_lease", side_effect=check_then_complete
        ):
            attempt = task.complete_with_attempt(exit_code=1, error="run failed")

        task.refresh_from_db()
        attempt.refresh_from_db()
        assert task.status == Task.Status.COMPLETED
        assert attempt.exit_code == 0
        assert attempt.outcome == TaskAttempt.Outcome.SUCCESS
        assert attempt.error == ""
        assert attempt.error_fingerprint == ""
        assert task.attempts.count() == 1

    def test_lease_loss_after_success_write_cannot_publish_success(self) -> None:
        ticket = Ticket.objects.create()
        task = Task.objects.create(ticket=ticket, session=Session.objects.create(ticket=ticket))
        task.claim(claimed_by="worker", claimed_by_session="run-1")
        original_create = TaskAttempt.objects.create
        published: list[str] = []

        def create_and_queue_publish(**kwargs: object) -> TaskAttempt:
            attempt = original_create(**kwargs)
            if kwargs["exit_code"] == 0 and kwargs["error"] == "":
                transaction.on_commit(lambda: published.append("success"))
            return attempt

        with (
            self.captureOnCommitCallbacks(execute=True),
            mock.patch.object(TaskAttempt.objects, "create", side_effect=create_and_queue_publish),
            mock.patch.object(Task, "complete", side_effect=LeaseLostError("rival claimed task")),
        ):
            attempt = task.complete_with_attempt(exit_code=0)

        attempt.refresh_from_db()
        assert published == []
        assert attempt.error.startswith("stuck_loop:")
        assert task.attempts.count() == 1

    def test_strict_lease_rolls_back_attempt_when_completion_loses_lease(self) -> None:
        ticket = Ticket.objects.create()
        task = Task.objects.create(ticket=ticket, session=Session.objects.create(ticket=ticket))

        with (
            pytest.raises(LeaseLostError),
            transaction.atomic(),
            mock.patch.object(Task, "complete", side_effect=LeaseLostError("rival claimed task")),
        ):
            complete_with_strict_lease(task, {"summary": "review skipped"})

        assert task.attempts.count() == 0

    def test_complete_with_attempt_retries_a_locked_attempt_write(self) -> None:
        ticket = Ticket.objects.create()
        task = Task.objects.create(ticket=ticket, session=Session.objects.create(ticket=ticket))
        original_create = TaskAttempt.objects.create
        calls = 0

        def create_after_lock(**kwargs: object) -> TaskAttempt:
            nonlocal calls
            calls += 1
            if calls == 1:
                message = "database is locked"
                raise OperationalError(message)
            return original_create(**kwargs)

        with mock.patch.object(TaskAttempt.objects, "create", side_effect=create_after_lock):
            attempt = task.complete_with_attempt(exit_code=1, error="run failed")

        assert calls == 2
        assert attempt.pk is not None
        assert task.attempts.count() == 1
