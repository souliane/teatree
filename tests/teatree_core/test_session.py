import pytest
from django.test import TestCase
from django.utils import timezone

from teatree.core.models import Session, Ticket
from teatree.core.session import SessionNotFound, get_active_session


class TestGetActiveSession(TestCase):
    def test_get_active_session_raises_when_no_active_session(self) -> None:
        with pytest.raises(SessionNotFound):
            get_active_session()

    def test_get_active_session_returns_active_session(self) -> None:
        ticket = Ticket.objects.create()
        session = Session.objects.create(ticket=ticket, agent_id="agent-1")

        result = get_active_session()

        assert result == session

    def test_get_active_session_ignores_ended_sessions(self) -> None:
        ticket = Ticket.objects.create()
        Session.objects.create(
            ticket=ticket,
            agent_id="agent-1",
            ended_at=timezone.make_aware(timezone.datetime(2020, 1, 1, 0, 0, 0)),
        )

        with pytest.raises(SessionNotFound):
            get_active_session()

    def test_get_active_session_returns_most_recent_active(self) -> None:
        ticket = Ticket.objects.create()
        Session.objects.create(ticket=ticket, agent_id="agent-1")
        session2 = Session.objects.create(ticket=ticket, agent_id="agent-2")

        result = get_active_session()

        assert result == session2
