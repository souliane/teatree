"""Integration test for ``PendingTasksScanner`` against the real Task model."""

from django.test import TestCase

from teatree.core.models.session import Session
from teatree.core.models.task import Task
from teatree.core.models.ticket import Ticket
from teatree.loop.scanners.pending_tasks import PendingTasksScanner


class PendingTasksScannerTests(TestCase):
    def _ticket(self, issue_url: str) -> Ticket:
        return Ticket.objects.create(issue_url=issue_url, overlay="acme")

    def _session(self, ticket: Ticket) -> Session:
        return Session.objects.create(ticket=ticket, agent_id="test-agent")

    def test_emits_signal_per_pending_task(self) -> None:
        ticket = self._ticket("https://example.com/issues/1")
        session = self._session(ticket)
        Task.objects.create(ticket=ticket, session=session, phase="reviewing", status=Task.Status.PENDING)
        Task.objects.create(ticket=ticket, session=session, phase="coding", status=Task.Status.COMPLETED)
        signals = PendingTasksScanner().scan()
        assert len(signals) == 1
        assert signals[0].kind == "pending_task"
        assert signals[0].payload["phase"] == "reviewing"

    def test_respects_limit(self) -> None:
        ticket = self._ticket("https://example.com/issues/2")
        session = self._session(ticket)
        for _ in range(3):
            Task.objects.create(ticket=ticket, session=session, phase="coding", status=Task.Status.PENDING)
        signals = PendingTasksScanner(limit=2).scan()
        assert len(signals) == 2
