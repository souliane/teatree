"""Integration test for ``PendingTasksScanner`` against the real Task model."""

from datetime import timedelta

from django.test import TestCase
from django.utils import timezone

from teatree.core.models.session import Session
from teatree.core.models.task import Task
from teatree.core.models.ticket import Ticket
from teatree.loop.scanners.pending_tasks import PendingTasksScanner
from tests.factories import PullRequestFactory


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
        assert signals[0].payload["status"] == Task.Status.PENDING

    def test_respects_limit(self) -> None:
        ticket = self._ticket("https://example.com/issues/2")
        session = self._session(ticket)
        for _ in range(3):
            Task.objects.create(ticket=ticket, session=session, phase="coding", status=Task.Status.PENDING)
        signals = PendingTasksScanner(limit=2).scan()
        assert len(signals) == 2

    def test_parked_started_pull_request_is_visible_before_unstarted_work(self) -> None:
        unstarted = self._ticket("https://example.com/issues/20")
        Task.objects.create(ticket=unstarted, session=self._session(unstarted), phase="coding")
        started = self._ticket("https://example.com/issues/21")
        parked = Task.objects.create(
            ticket=started,
            session=self._session(started),
            phase="coding",
            not_before=timezone.now() + timedelta(days=1),
        )
        PullRequestFactory(ticket=started, state="open")

        signals = PendingTasksScanner(limit=1).scan()

        assert [signal.payload["task_id"] for signal in signals] == [parked.pk]

    def test_summary_uses_human_subject_not_bare_phase(self) -> None:
        ticket = self._ticket("https://example.com/issues/3")
        session = self._session(ticket)
        Task.objects.create(
            ticket=ticket,
            session=session,
            phase="short_describe",
            subject="Provision smoke: t3-teatree",
            status=Task.Status.PENDING,
        )
        signals = PendingTasksScanner().scan()
        assert signals[0].payload["subject"] == "Provision smoke: t3-teatree"
        assert "(short_describe)" not in signals[0].summary
        assert "Provision smoke: t3-teatree" in signals[0].summary
