"""The compact SDK-equivalent cost chip anchor on the statusline."""

import pytest
from django.utils import timezone

from teatree.core.models import Session, Task, TaskAttempt
from teatree.loop.rendering import cost_chip_lines
from tests.factories import TicketFactory

pytestmark = pytest.mark.django_db


def _headless_attempt(task: Task, *, cost: float) -> TaskAttempt:
    return TaskAttempt.objects.create(
        task=task,
        execution_target=Task.ExecutionTarget.HEADLESS,
        cost_usd=cost,
        started_at=timezone.now(),
    )


class TestCostChipLines:
    def setup_method(self) -> None:
        self.ticket = TicketFactory()
        self.session = Session.objects.create(ticket=self.ticket)
        self.task = Task.objects.create(ticket=self.ticket, session=self.session)

    def test_silenced_when_no_headless_cost(self) -> None:
        assert cost_chip_lines() == []

    def test_renders_compact_chip(self) -> None:
        _headless_attempt(self.task, cost=30.0)
        _headless_attempt(self.task, cost=18.0)
        assert cost_chip_lines() == ["SDK ≈$48/$200"]

    def test_chip_stays_tiny_at_high_spend(self) -> None:
        _headless_attempt(self.task, cost=1234.0)
        assert cost_chip_lines() == ["SDK ≈$1234/$200"]

    def test_excludes_interactive_attempts(self) -> None:
        TaskAttempt.objects.create(
            task=self.task,
            execution_target=Task.ExecutionTarget.INTERACTIVE,
            cost_usd=500.0,
            started_at=timezone.now(),
        )
        assert cost_chip_lines() == []
