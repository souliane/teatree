from datetime import timedelta

from django.test import TestCase
from django.utils import timezone

from teatree.core.factory.operational_health import collect_signals
from teatree.core.factory.parked_tickets import aged_parked_tasks, silent_freeze_tasks
from teatree.core.modelkit.task_failure_taxonomy import HALT_STAMP, FailureKind
from teatree.core.models import Session, Task, Ticket


class ParkedTicketSelectionTest(TestCase):
    def _failed(self, *, reason: str = "", kind: str = "", age_days: int = 15) -> Task:
        ticket = Ticket.objects.create(
            issue_url=f"https://example.com/issues/{Ticket.objects.count() + 1}", state=Ticket.State.WORK_STARTED
        )
        session = Session.objects.create(overlay="test", ticket=ticket)
        task = Task.objects.create(
            ticket=ticket, session=session, status=Task.Status.FAILED, execution_reason=reason, failure_kind=kind
        )
        Task.objects.filter(pk=task.pk).update(created_at=timezone.now() - timedelta(days=age_days))
        task.refresh_from_db()
        return task

    def test_halted_and_cancelled_are_parked(self) -> None:
        halted = self._failed(reason=HALT_STAMP)
        cancelled = self._failed(kind=FailureKind.CANCELLED)
        silent = self._failed()
        assert {task.pk for task in aged_parked_tasks()} == {halted.pk, cancelled.pk}
        assert [task.pk for task in silent_freeze_tasks()] == [silent.pk]

    def test_newer_task_and_recent_park_do_not_qualify(self) -> None:
        superseded = self._failed(reason=HALT_STAMP)
        self._failed(reason=HALT_STAMP, age_days=1)
        Task.objects.create(ticket=superseded.ticket, session=superseded.session, status=Task.Status.PENDING)
        assert aged_parked_tasks() == []

    def test_synthetic_ticket_is_not_deliverable_work(self) -> None:
        task = self._failed(reason=HALT_STAMP)
        Ticket.objects.filter(pk=task.ticket_id).update(issue_url="loop://test")
        assert aged_parked_tasks() == []

    def test_aged_parks_raise_one_owner_decision_signal(self) -> None:
        self._failed(reason=HALT_STAMP)
        self._failed(kind=FailureKind.CANCELLED)
        (signal,) = [row for row in collect_signals().signals if row.fingerprint == "parked-tickets-await-owner"]
        assert signal.fingerprint == "parked-tickets-await-owner"
        assert "retire or keep" in signal.summary
