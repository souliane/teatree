"""DB-backed tests for ``ActiveTicketsScanner``."""

from django.test import TestCase

from teatree.core.modelkit.phases import SHORT_DESCRIBE_PHASE
from teatree.core.models import Task
from teatree.core.models.session import Session
from teatree.core.models.ticket import Ticket
from teatree.loop.scanners.active_tickets import ActiveTicketsScanner


class TestActiveTicketsScanner(TestCase):
    def test_emits_signal_for_non_terminal_tickets(self) -> None:
        Ticket.objects.create(overlay="acme", issue_url="https://x/1", state="started")
        Ticket.objects.create(overlay="acme", issue_url="https://x/2", state="delivered")
        signals = ActiveTicketsScanner().scan()
        assert len(signals) == 1
        assert signals[0].kind == "ticket.active"
        assert signals[0].payload["state"] == "started"

    def test_filters_by_overlay_name(self) -> None:
        Ticket.objects.create(overlay="acme", issue_url="https://x/1", state="coded")
        Ticket.objects.create(overlay="other", issue_url="https://x/2", state="coded")
        signals = ActiveTicketsScanner(overlay_name="acme").scan()
        assert len(signals) == 1
        assert signals[0].payload["ticket_number"] == "1"

    def test_excludes_ignored_tickets(self) -> None:
        Ticket.objects.create(overlay="acme", issue_url="https://x/1", state="ignored")
        assert ActiveTicketsScanner().scan() == []


class TestShortDescribeSelfHeal(TestCase):
    """A ``short_describe`` task that COMPLETED without writing the field must re-enqueue (#3570).

    Pre-#3570 the agentic dispatch left ``short_description`` blank on a COMPLETED task, and the
    dedup filter included COMPLETED — so the ticket was suppressed from re-enqueue for good. With
    the deterministic runner now guaranteeing a non-blank write for any titled ticket, dropping
    COMPLETED from the dedup is churn-safe: the field goes non-blank after one heal cycle and the
    scanner's blank-field gate stops firing. In-flight (PENDING/CLAIMED) tasks still suppress.
    """

    def _ticket(self) -> Ticket:
        return Ticket.objects.create(
            overlay="acme",
            issue_url="https://x/1",
            state="started",
            extra={"issue_title": "add dark mode toggle"},
            short_description="",
        )

    def _prior_task(self, ticket: Ticket, status: str) -> Task:
        session = Session.objects.create(ticket=ticket, agent_id="short-describe")
        return Task.objects.create(
            ticket=ticket,
            session=session,
            phase=SHORT_DESCRIBE_PHASE,
            execution_target=Task.ExecutionTarget.HEADLESS,
            status=status,
        )

    def _short_describe_tasks(self, ticket: Ticket):
        return Task.objects.filter(ticket=ticket, phase=SHORT_DESCRIBE_PHASE)

    def test_completed_but_blank_ticket_reenqueues(self) -> None:
        ticket = self._ticket()
        self._prior_task(ticket, Task.Status.COMPLETED)
        ActiveTicketsScanner().scan()
        assert self._short_describe_tasks(ticket).filter(status=Task.Status.PENDING).count() == 1

    def test_inflight_task_still_suppresses(self) -> None:
        ticket = self._ticket()
        self._prior_task(ticket, Task.Status.CLAIMED)
        ActiveTicketsScanner().scan()
        assert self._short_describe_tasks(ticket).count() == 1

    def test_populated_field_never_enqueues(self) -> None:
        ticket = self._ticket()
        ticket.short_description = "dark mode toggle"
        ticket.save(update_fields=["short_description"])
        ActiveTicketsScanner().scan()
        assert self._short_describe_tasks(ticket).count() == 0
