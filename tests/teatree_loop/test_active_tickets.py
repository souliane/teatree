"""DB-backed tests for ``ActiveTicketsScanner``."""

from django.test import TestCase

from teatree.core.modelkit.phases import SHORT_DESCRIBE_PHASE
from teatree.core.models.session import Session
from teatree.core.models.task import Task
from teatree.core.models.ticket import Ticket
from teatree.loop.scanners.active_tickets import SHORT_DESCRIBE_MAX_ATTEMPTS, ActiveTicketsScanner


def _describable_ticket(number: str = "1", *, short_description: str = "") -> Ticket:
    return Ticket.objects.create(
        overlay="acme",
        issue_url=f"https://x/{number}",
        state="started",
        short_description=short_description,
        extra={"issue_title": "Cached tracker title"},
    )


def _short_describe_task(ticket: Ticket, status: str) -> Task:
    task = Task.objects.create(
        ticket=ticket,
        session=Session.objects.create(ticket=ticket, agent_id="short-describe"),
        phase=SHORT_DESCRIBE_PHASE,
        execution_target=Task.ExecutionTarget.HEADLESS,
    )
    Task.objects.filter(pk=task.pk).update(status=status)
    return task


def _short_describe_tasks(ticket: Ticket) -> int:
    return Task.objects.filter(ticket=ticket, phase=SHORT_DESCRIBE_PHASE).count()


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


class TestShortDescribeEnqueueHonoursTheArtifact(TestCase):
    """A COMPLETED task is only "handled" when the description it owed actually landed."""

    def test_completed_task_with_blank_description_is_re_enqueued(self) -> None:
        ticket = _describable_ticket()
        _short_describe_task(ticket, Task.Status.COMPLETED)

        ActiveTicketsScanner(overlay_name="acme").scan()

        assert _short_describe_tasks(ticket) == 2

    def test_completed_task_with_populated_description_is_not_re_enqueued(self) -> None:
        ticket = _describable_ticket(short_description="cached-title summary")
        _short_describe_task(ticket, Task.Status.COMPLETED)

        ActiveTicketsScanner(overlay_name="acme").scan()

        assert _short_describe_tasks(ticket) == 1

    def test_in_flight_task_still_suppresses_a_duplicate(self) -> None:
        ticket = _describable_ticket()
        _short_describe_task(ticket, Task.Status.PENDING)

        ActiveTicketsScanner(overlay_name="acme").scan()

        assert _short_describe_tasks(ticket) == 1


class TestShortDescribeAttemptCeiling(TestCase):
    """The honest dedup re-enqueues, but never without bound."""

    def test_enqueues_stop_after_the_ceiling_and_never_resume(self) -> None:
        ticket = _describable_ticket()
        scanner = ActiveTicketsScanner(overlay_name="acme")

        for _ in range(SHORT_DESCRIBE_MAX_ATTEMPTS + 3):
            Task.objects.filter(ticket=ticket, phase=SHORT_DESCRIBE_PHASE).update(status=Task.Status.FAILED)
            scanner.scan()

        assert _short_describe_tasks(ticket) == SHORT_DESCRIBE_MAX_ATTEMPTS

    def test_a_lying_completion_does_not_refund_the_budget(self) -> None:
        ticket = _describable_ticket()
        scanner = ActiveTicketsScanner(overlay_name="acme")

        for _ in range(SHORT_DESCRIBE_MAX_ATTEMPTS + 3):
            Task.objects.filter(ticket=ticket, phase=SHORT_DESCRIBE_PHASE).update(status=Task.Status.COMPLETED)
            scanner.scan()

        assert _short_describe_tasks(ticket) == SHORT_DESCRIBE_MAX_ATTEMPTS

    def test_a_spent_budget_still_renders_the_cached_title(self) -> None:
        ticket = _describable_ticket()
        ticket.merge_extra(set_keys={"phase_attempts": {SHORT_DESCRIBE_PHASE: SHORT_DESCRIBE_MAX_ATTEMPTS}})

        signals = ActiveTicketsScanner(overlay_name="acme").scan()

        assert _short_describe_tasks(ticket) == 0
        assert signals[0].payload["title"] == "Cached tracker title"
