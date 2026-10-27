from datetime import timedelta

from django.test import TestCase
from django.utils import timezone

from teatree.core.models import Ticket
from teatree.core.models.transition import TicketTransition
from tests.factories import TaskAttemptFactory, TaskFactory


class TestLastActivity(TestCase):
    def test_no_attempt_or_transition_has_no_activity(self) -> None:
        ticket = Ticket.objects.create(issue_url="https://github.com/o/r/issues/56")
        assert ticket.last_activity_at() is None

    def test_newest_attempt_or_transition_decides_activity(self) -> None:
        ticket = Ticket.objects.create(issue_url="https://github.com/o/r/issues/56")
        attempt = TaskAttemptFactory(task=TaskFactory(ticket=ticket))
        old = timezone.now() - timedelta(days=30)
        TicketTransition.objects.create(ticket=ticket, from_state="not_started", to_state="scoped")
        TicketTransition.objects.filter(ticket=ticket).update(created_at=old)

        assert ticket.last_activity_at() == attempt.started_at
