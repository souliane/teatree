"""Delivery-ownership lease for hand-dispatched external delivery (#2104).

A hand-dispatched delivery agent (``workspace ticket``) implements directly and
never claims the loop's auto-scheduled planner/reviewer, so the loop must NOT
re-derive that lifecycle work for a unit under active external delivery. The
lease is TTL'd so a crashed external owner cannot wedge the loop (mirrors
``LoopLease``/``Task`` lease semantics). The loop's own FSM never stamps it, so
the predicate is False on every loop-driven ticket.
"""

from django.test import TestCase

from teatree.core.models import Ticket
from teatree.core.models.external_delivery import mark_external_delivery, under_external_delivery


class TestExternalDeliveryLease(TestCase):
    def test_unstamped_ticket_is_not_under_external_delivery(self) -> None:
        ticket = Ticket.objects.create(overlay="test")
        assert under_external_delivery(ticket) is False

    def test_stamped_ticket_is_under_external_delivery_within_ttl(self) -> None:
        ticket = Ticket.objects.create(overlay="test")
        mark_external_delivery(ticket)
        ticket.refresh_from_db()
        assert under_external_delivery(ticket) is True

    def test_expired_lease_is_not_under_external_delivery(self) -> None:
        ticket = Ticket.objects.create(overlay="test")
        mark_external_delivery(ticket, lease_seconds=-1)
        ticket.refresh_from_db()
        assert under_external_delivery(ticket) is False

    def test_lease_persists_through_locked_rmw_without_clobbering(self) -> None:
        ticket = Ticket.objects.create(overlay="test", extra={"keep": 1})
        mark_external_delivery(ticket)
        ticket.refresh_from_db()
        assert ticket.extra["keep"] == 1
        assert "external_delivery" in ticket.extra

    def test_malformed_expires_at_is_treated_as_no_lease(self) -> None:
        ticket = Ticket.objects.create(overlay="test", extra={"external_delivery": {"expires_at": "not-a-date"}})
        assert under_external_delivery(ticket) is False

    def test_missing_expires_at_is_treated_as_no_lease(self) -> None:
        ticket = Ticket.objects.create(overlay="test", extra={"external_delivery": {"at": "2026-01-01T00:00:00"}})
        assert under_external_delivery(ticket) is False

    def test_non_dict_lease_value_is_treated_as_no_lease(self) -> None:
        ticket = Ticket.objects.create(overlay="test", extra={"external_delivery": "garbage"})
        assert under_external_delivery(ticket) is False
