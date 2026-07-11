"""Ticket drawer + legal-only FSM-transition POST executed via the guarded method (#3162)."""

from django.test import Client, TestCase
from django.urls import reverse

from teatree.core.models.ticket import Ticket
from teatree.core.models.transition import TicketTransition
from teatree.dash.ticket_detail import legal_transition_names
from tests.factories import TicketFactory

State = Ticket.State


class LegalTransitionSetTestCase(TestCase):
    def test_only_legal_transitions_are_offered(self) -> None:
        ticket = TicketFactory(state=State.NOT_STARTED)
        names = legal_transition_names(ticket)
        assert "scope" in names
        # ship is illegal from NOT_STARTED — it must not be offered.
        assert "ship" not in names


class TicketTransitionPostTestCase(TestCase):
    def setUp(self) -> None:
        self.ticket = TicketFactory(state=State.NOT_STARTED)
        self.url = reverse("dash:ticket_transition", args=[self.ticket.pk])

    def test_legal_transition_advances_state_via_model_method(self) -> None:
        self.client.post(self.url, {"action": "scope"})
        self.ticket.refresh_from_db()
        assert self.ticket.state == State.SCOPED
        # the guarded method fired, so the post_transition signal recorded a row.
        assert TicketTransition.objects.filter(ticket=self.ticket, to_state=State.SCOPED).exists()

    def test_illegal_transition_is_rejected(self) -> None:
        resp = self.client.post(self.url, {"action": "ship"})
        assert resp.status_code == 400
        self.ticket.refresh_from_db()
        assert self.ticket.state == State.NOT_STARTED

    def test_unknown_action_is_rejected(self) -> None:
        resp = self.client.post(self.url, {"action": "teleport"})
        assert resp.status_code == 400

    def test_transition_is_audited(self) -> None:
        with self.assertLogs("teatree.dash.audit", level="INFO") as logs:
            self.client.post(self.url, {"action": "scope"})
        assert any("action=ticket:scope" in line for line in logs.output)

    def test_csrf_is_enforced(self) -> None:
        csrf_client = Client(enforce_csrf_checks=True)
        resp = csrf_client.post(self.url, {"action": "scope"})
        assert resp.status_code == 403


class TicketDrawerGetTestCase(TestCase):
    def test_drawer_renders_history_mermaid_and_actions(self) -> None:
        ticket = TicketFactory(state=State.STARTED, short_description="drawer subject")
        TicketTransition.objects.create(
            ticket=ticket, from_state=State.SCOPED, to_state=State.STARTED, triggered_by="start"
        )
        resp = self.client.get(reverse("dash:ticket_drawer", args=[ticket.pk]))
        assert resp.status_code == 200
        body = resp.content.decode()
        assert "drawer subject" in body
        assert "stateDiagram-v2" in body
        assert "start" in body

    def test_drawer_404_for_missing_ticket(self) -> None:
        resp = self.client.get(reverse("dash:ticket_drawer", args=[999999]))
        assert resp.status_code == 404
