"""External-owner FSM seams refresh a live delivery lease (#2217).

A hand-delivery agent advances its ticket through the CLI (`ticket plan`,
`ticket transition`). Each such action refreshes the live `external_delivery`
lease so a delivery longer than `LEASE_SECONDS` cannot lapse mid-delivery and
re-open the double-dispatch race. The refresh is a strict extend: it is a no-op
on a ticket with no live lease, so the loop's own FSM transitions never claim a
unit.
"""

from datetime import datetime
from typing import cast

import pytest
from django.core.management import call_command
from django.test import TestCase

from teatree.core.models import Ticket
from teatree.core.models.external_delivery import mark_external_delivery, under_external_delivery
from teatree.core.models.plan_artifact import PlanArtifact

pytestmark = pytest.mark.filterwarnings(
    "ignore:In Typer, only the parameter 'autocompletion' is supported.*:DeprecationWarning",
)


def _expires_at(ticket: Ticket) -> datetime:
    ticket.refresh_from_db()
    return datetime.fromisoformat(ticket.extra["external_delivery"]["expires_at"])


class TicketPlanRefreshesLeaseTest(TestCase):
    def test_plan_refreshes_a_live_external_delivery_lease(self) -> None:
        ticket = Ticket.objects.create(overlay="test", state=Ticket.State.STARTED)
        mark_external_delivery(ticket, lease_seconds=10)
        before = _expires_at(ticket)

        call_command("ticket", "plan", str(ticket.pk), "implement the fix")

        ticket.refresh_from_db()
        assert ticket.state == Ticket.State.PLANNED
        assert _expires_at(ticket) > before
        assert under_external_delivery(ticket) is True


class TicketTransitionRefreshesLeaseTest(TestCase):
    def _planned_ticket_under_delivery(self) -> Ticket:
        ticket = Ticket.objects.create(overlay="test", state=Ticket.State.STARTED)
        PlanArtifact.record(ticket=ticket, plan_text="plan", recorded_by="operator")
        ticket.plan()
        ticket.save()
        mark_external_delivery(ticket, lease_seconds=10)
        return ticket

    def test_transition_refreshes_a_live_external_delivery_lease(self) -> None:
        ticket = self._planned_ticket_under_delivery()
        before = _expires_at(ticket)

        call_command("ticket", "transition", str(ticket.pk), "code")

        ticket.refresh_from_db()
        assert ticket.state == Ticket.State.CODED
        assert _expires_at(ticket) > before
        assert under_external_delivery(ticket) is True

    def test_transition_does_not_stamp_a_lease_on_a_loop_driven_ticket(self) -> None:
        # No lease present: a loop-driven transition must not create one, or the
        # dispatch chokepoint would wrongly skip the unit.
        ticket = Ticket.objects.create(overlay="test", state=Ticket.State.STARTED)
        PlanArtifact.record(ticket=ticket, plan_text="plan", recorded_by="operator")
        ticket.plan()
        ticket.save()

        result = cast("dict[str, object]", call_command("ticket", "transition", str(ticket.pk), "code"))

        ticket.refresh_from_db()
        assert result["state"] == Ticket.State.CODED
        assert "external_delivery" not in (ticket.extra or {})
        assert under_external_delivery(ticket) is False
