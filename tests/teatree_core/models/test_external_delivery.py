"""Delivery-ownership lease for hand-dispatched external delivery (#2104).

A hand-dispatched delivery agent (``workspace ticket``) implements directly and
never claims the loop's auto-scheduled planner/reviewer, so the loop must NOT
re-derive that lifecycle work for a unit under active external delivery. The
lease is TTL'd so a crashed external owner cannot wedge the loop (mirrors
``LoopLease``/``Task`` lease semantics). The loop's own FSM never stamps it, so
the predicate is False on every loop-driven ticket.
"""

from datetime import datetime

from django.test import TestCase

from teatree.core.models import Ticket
from teatree.core.models.external_delivery import (
    live_external_delivery_q,
    mark_external_delivery,
    refresh_external_delivery_if_active,
    under_external_delivery,
)


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


class TestLiveExternalDeliveryQ(TestCase):
    """The DB-layer Q must agree with the Python predicate at every boundary.

    ``_dispatchable_filter`` excludes tickets under a live lease via this Q, so a
    divergence between the Q and ``under_external_delivery`` would silently
    re-open the double-dispatch race for whichever case diverges.
    """

    def _live(self) -> Ticket:
        ticket = Ticket.objects.create(overlay="test")
        mark_external_delivery(ticket)
        ticket.refresh_from_db()
        return ticket

    def _expired(self) -> Ticket:
        ticket = Ticket.objects.create(overlay="test")
        mark_external_delivery(ticket, lease_seconds=-1)
        ticket.refresh_from_db()
        return ticket

    def _absent(self) -> Ticket:
        return Ticket.objects.create(overlay="test")

    def _malformed(self) -> Ticket:
        return Ticket.objects.create(overlay="test", extra={"external_delivery": {"expires_at": "not-a-date"}})

    def _terminal_live_lease(self) -> Ticket:
        ticket = Ticket.objects.create(overlay="test", state=Ticket.State.MERGED)
        mark_external_delivery(ticket)
        ticket.refresh_from_db()
        return ticket

    def test_live_lease_ticket_is_selected_by_the_q(self) -> None:
        ticket = self._live()
        assert Ticket.objects.filter(live_external_delivery_q(field_prefix="")).filter(pk=ticket.pk).exists()

    def test_expired_lease_ticket_is_not_selected_by_the_q(self) -> None:
        ticket = self._expired()
        assert not Ticket.objects.filter(live_external_delivery_q(field_prefix="")).filter(pk=ticket.pk).exists()

    def test_absent_lease_ticket_is_not_selected_by_the_q(self) -> None:
        ticket = self._absent()
        assert not Ticket.objects.filter(live_external_delivery_q(field_prefix="")).filter(pk=ticket.pk).exists()

    def test_malformed_lease_ticket_is_not_selected_by_the_q(self) -> None:
        ticket = self._malformed()
        assert not Ticket.objects.filter(live_external_delivery_q(field_prefix="")).filter(pk=ticket.pk).exists()

    def test_q_membership_agrees_with_python_predicate_for_every_fixture(self) -> None:
        fixtures = {
            "live": self._live(),
            "expired": self._expired(),
            "absent": self._absent(),
            "malformed": self._malformed(),
            "terminal_live_lease": self._terminal_live_lease(),
        }
        selected_pks = set(
            Ticket.objects.filter(live_external_delivery_q(field_prefix="")).values_list("pk", flat=True)
        )
        for name, ticket in fixtures.items():
            in_q = ticket.pk in selected_pks
            via_predicate = under_external_delivery(ticket)
            assert in_q == via_predicate, f"Q/predicate divergence for {name}: q={in_q} predicate={via_predicate}"


class TestRefreshExternalDeliveryIfActive(TestCase):
    """An actively-delivering owner's FSM activity must extend the lease (#2217).

    ``LEASE_SECONDS`` is one hour; a hand delivery that outruns it would lapse the
    lease while the owner is still working, re-opening the double-dispatch race.
    The external-owner FSM seams (``ticket plan``/``transition``) refresh the
    lease on each action, so an active owner never lapses while a CRASHED owner —
    which stops touching the seams — still self-reaps on TTL.
    """

    def _expires_at(self, ticket: Ticket) -> datetime:
        ticket.refresh_from_db()
        return datetime.fromisoformat(ticket.extra["external_delivery"]["expires_at"])

    def test_refresh_extends_expiry_for_an_actively_delivered_ticket(self) -> None:
        ticket = Ticket.objects.create(overlay="test")
        mark_external_delivery(ticket, lease_seconds=10)
        before = self._expires_at(ticket)
        refresh_external_delivery_if_active(ticket)
        after = self._expires_at(ticket)
        assert after > before
        assert under_external_delivery(ticket) is True

    def test_refresh_is_a_noop_on_a_ticket_with_no_lease(self) -> None:
        # The loop's own FSM never stamps a lease; refresh must not create one,
        # or every loop-driven transition would spuriously claim the unit.
        ticket = Ticket.objects.create(overlay="test")
        refresh_external_delivery_if_active(ticket)
        ticket.refresh_from_db()
        assert "external_delivery" not in (ticket.extra or {})
        assert under_external_delivery(ticket) is False

    def test_refresh_does_not_resurrect_an_expired_lease(self) -> None:
        # A crashed owner's lease has already lapsed; refresh must not revive it,
        # preserving the #2104 self-reap that lets the loop resume.
        ticket = Ticket.objects.create(overlay="test")
        mark_external_delivery(ticket, lease_seconds=-1)
        refresh_external_delivery_if_active(ticket)
        ticket.refresh_from_db()
        assert under_external_delivery(ticket) is False
