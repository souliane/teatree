import pytest
from django.test import TestCase, override_settings

from teatree.agents.headless_budget import TicketBudget
from teatree.core.models import Session, Task, TaskAttempt, Ticket


class TestTicketBudget(TestCase):
    """Per-ticket cumulative cost-cap consumer (#885 / #398-4)."""

    def setUp(self) -> None:
        self.ticket = Ticket.objects.create()
        self.session = Session.objects.create(ticket=self.ticket)
        self.task = Task.objects.create(ticket=self.ticket, session=self.session)

    def test_disabled_budget_never_refuses(self) -> None:
        TaskAttempt.objects.create(task=self.task, cost_usd=9999.0)
        budget = TicketBudget(max_cost_usd=0.0)
        assert budget.breach_reason(self.ticket) is None

    def test_under_cap_does_not_refuse(self) -> None:
        TaskAttempt.objects.create(task=self.task, cost_usd=2.0)
        budget = TicketBudget(max_cost_usd=5.0)
        assert budget.breach_reason(self.ticket) is None

    def test_over_cap_refuses_with_observed_total(self) -> None:
        TaskAttempt.objects.create(task=self.task, cost_usd=4.0)
        TaskAttempt.objects.create(task=self.task, cost_usd=3.5)
        budget = TicketBudget(max_cost_usd=5.0)
        reason = budget.breach_reason(self.ticket)
        assert reason is not None
        assert "budget" in reason
        assert "7.50" in reason
        assert "5.00" in reason

    def test_sums_across_all_tasks_of_the_ticket(self) -> None:
        other_session = Session.objects.create(ticket=self.ticket)
        other_task = Task.objects.create(ticket=self.ticket, session=other_session)
        TaskAttempt.objects.create(task=self.task, cost_usd=3.0)
        TaskAttempt.objects.create(task=other_task, cost_usd=3.0)
        budget = TicketBudget(max_cost_usd=5.0)
        reason = budget.breach_reason(self.ticket)
        assert reason is not None
        assert "6.00" in reason

    def test_ignores_other_tickets(self) -> None:
        other_ticket = Ticket.objects.create()
        other_session = Session.objects.create(ticket=other_ticket)
        other_task = Task.objects.create(ticket=other_ticket, session=other_session)
        TaskAttempt.objects.create(task=other_task, cost_usd=99.0)
        TaskAttempt.objects.create(task=self.task, cost_usd=1.0)
        budget = TicketBudget(max_cost_usd=5.0)
        assert budget.breach_reason(self.ticket) is None

    def test_from_settings_reads_configured_cap(self) -> None:
        with override_settings(TEATREE_TICKET_BUDGET={"max_cost_usd": 12.5}):
            budget = TicketBudget.from_settings()
        assert budget.max_cost_usd == pytest.approx(12.5)

    def test_from_settings_defaults_to_disabled(self) -> None:
        with override_settings():
            from django.conf import settings  # noqa: PLC0415

            if hasattr(settings, "TEATREE_TICKET_BUDGET"):
                del settings.TEATREE_TICKET_BUDGET
            budget = TicketBudget.from_settings()
        assert budget.max_cost_usd == pytest.approx(0.0)
