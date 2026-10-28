from django.test import TestCase
from django.utils import timezone

from teatree.core.factory.halted_retries import halted_retry_signals
from teatree.core.models import Session, Task, TaskAttempt, Ticket
from tests.factories import planned_ticket


class TestHaltedRetrySignals(TestCase):
    def test_halt_names_cause_and_clears_on_successor(self) -> None:
        ticket = planned_ticket()
        session = Session.objects.create(ticket=ticket, overlay=ticket.overlay)
        halted = Task.objects.create(
            ticket=ticket,
            session=session,
            phase="coding",
            status=Task.Status.FAILED,
            execution_reason="[repair-halt-parked]",
        )
        TaskAttempt.objects.create(task=halted, ended_at=timezone.now(), exit_code=1, error="missing required evidence")

        (signal,) = halted_retry_signals().signals
        assert signal.kind == "halted_retries"
        assert "missing required evidence" in signal.summary

        Task.objects.create(ticket=ticket, session=session, phase="coding", status=Task.Status.COMPLETED)
        assert not halted_retry_signals().signals

    def test_halt_without_attempt_uses_task_failure_reason(self) -> None:
        ticket = planned_ticket()
        session = Session.objects.create(ticket=ticket, overlay=ticket.overlay)
        Task.objects.create(
            ticket=ticket,
            session=session,
            phase="coding",
            status=Task.Status.FAILED,
            execution_reason="[repair-halt-parked]",
            failure_reason="no recorded error",
        )

        (signal,) = halted_retry_signals().signals
        assert "no recorded error" in signal.summary
        assert signal.fingerprint.startswith(f"halted-retries:{ticket.pk}:coding:")

    def test_cancelled_and_settled_halts_are_not_live(self) -> None:
        ticket = planned_ticket()
        session = Session.objects.create(ticket=ticket, overlay=ticket.overlay)
        Task.objects.create(
            ticket=ticket,
            session=session,
            phase="coding",
            status=Task.Status.FAILED,
            execution_reason="[repair-halt-parked]",
            failure_kind="cancelled",
        )
        assert not halted_retry_signals().signals

        Task.objects.create(
            ticket=ticket,
            session=session,
            phase="coding",
            status=Task.Status.FAILED,
            execution_reason="[repair-halt-parked]",
        )
        ticket.state = Ticket.State.MERGED
        ticket.save(update_fields=["state"])
        assert not halted_retry_signals().signals
