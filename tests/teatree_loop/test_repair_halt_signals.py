"""Stopped retries remain visible as health signals for every affected ticket."""

from django.test import TestCase
from django.utils import timezone

from teatree.core.factory.halted_retries import halted_retry_signals
from teatree.core.models import Session, Task, TaskAttempt, Ticket
from teatree.core.models.deferred_question import DeferredQuestion
from teatree.loop.transient_requeue import requeue_transient_failed

_SHARED_CONDITION = "missing required evidence for phase 'reviewing'"


def _failed_task_on_new_ticket(*, phase: str, error: str) -> Task:
    ticket = Ticket.objects.create(role=Ticket.Role.AUTHOR, state=Ticket.State.TESTED)
    session = Session.objects.create(ticket=ticket, agent_id=phase)
    task = Task.objects.create(ticket=ticket, session=session, phase=phase, status=Task.Status.FAILED)
    TaskAttempt.objects.create(
        task=task,
        ended_at=timezone.now(),
        exit_code=1,
        error=error,
    )
    Task.objects.filter(pk=task.pk).update(status=Task.Status.FAILED)
    return task


class TestRepairHaltSignals(TestCase):
    def test_same_root_cause_stays_visible_for_every_ticket_without_questions(self) -> None:
        for _ in range(3):
            _failed_task_on_new_ticket(phase="reviewing", error=_SHARED_CONDITION)

        requeue_transient_failed()

        assert len(halted_retry_signals().signals) == 3
        assert not DeferredQuestion.objects.exists()

    def test_different_root_causes_keep_distinct_health_signals(self) -> None:
        _failed_task_on_new_ticket(phase="reviewing", error=_SHARED_CONDITION)
        _failed_task_on_new_ticket(phase="reviewing", error="AssertionError: reviewer crashed")

        requeue_transient_failed()

        signals = halted_retry_signals().signals
        assert len(signals) == 2
        assert len({signal.fingerprint for signal in signals}) == 2
        assert not DeferredQuestion.objects.exists()
