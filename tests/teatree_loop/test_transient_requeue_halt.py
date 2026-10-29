"""Terminal architectural-review failures are reported without a repair question."""

from unittest.mock import patch

from django.db import connection
from django.test import TestCase
from django.utils import timezone

from teatree.agents.credential_policy import PLANS_ONLY_MARKER
from teatree.core.models import BotPing, DeferredQuestion, Session, Task, TaskAttempt, Ticket
from teatree.loop.transient_requeue import requeue_transient_failed


class TestArchitecturalReviewHalt(TestCase):
    def test_owner_notice_is_sent_outside_the_halt_transaction(self) -> None:
        ticket = Ticket.objects.create(issue_url="architectural-review://test", overlay="test")
        task = Task.objects.create(
            ticket=ticket,
            session=Session.objects.create(ticket=ticket),
            phase="architectural_review",
            status=Task.Status.FAILED,
            execution_reason=PLANS_ONLY_MARKER,
        )
        TaskAttempt.objects.create(task=task, ended_at=timezone.now(), exit_code=0, error="credential available: none")
        depth = len(connection.atomic_blocks)
        observed: list[int] = []

        def observe_notice(_task: Task, _reason: str) -> None:
            observed.append(len(connection.atomic_blocks))

        with (
            patch("teatree.loop.transient_requeue_halt.announce_failed", side_effect=observe_notice),
            self.captureOnCommitCallbacks(execute=True),
        ):
            assert requeue_transient_failed() == 0

        assert observed == [depth]

    def test_missing_credential_gives_one_failed_notice(self) -> None:
        ticket = Ticket.objects.create(issue_url="architectural-review://test", overlay="test")
        task = Task.objects.create(
            ticket=ticket,
            session=Session.objects.create(ticket=ticket),
            phase="architectural_review",
            status=Task.Status.FAILED,
            execution_reason=PLANS_ONLY_MARKER,
        )
        TaskAttempt.objects.create(task=task, ended_at=timezone.now(), exit_code=0, error="credential available: none")

        with self.captureOnCommitCallbacks(execute=True):
            assert requeue_transient_failed() == 0
            assert requeue_transient_failed() == 0
        assert DeferredQuestion.objects.count() == 0
        assert BotPing.objects.filter(idempotency_key=f"architectural-review:failed:{task.pk}").count() == 1
