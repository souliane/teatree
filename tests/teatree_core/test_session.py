from unittest.mock import patch

import pytest
from django.test import TestCase
from django.utils import timezone

from teatree.core.models import Session, Ticket
from teatree.core.session import SessionNotFound, get_active_session


class TestGetActiveSession(TestCase):
    def test_raises_session_not_found_when_no_active_session(self) -> None:
        with pytest.raises(SessionNotFound):
            get_active_session()

    def test_returns_active_session_for_current_agent(self) -> None:
        ticket = Ticket.objects.create()
        session = Session.objects.create(ticket=ticket, agent_id="test-agent-id")

        with patch("teatree.core.session.current_session_id", return_value="test-agent-id"):
            result = get_active_session()

        assert result.pk == session.pk

    def test_raises_session_not_found_when_session_ended(self) -> None:
        ticket = Ticket.objects.create()
        Session.objects.create(ticket=ticket, agent_id="ended-agent-id", ended_at=timezone.now())

        with (
            pytest.raises(SessionNotFound),
            patch("teatree.core.session.current_session_id", return_value="ended-agent-id"),
        ):
            get_active_session()
