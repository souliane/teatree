"""Mechanical action handlers — inline ticket transitions during a tick."""

from typing import cast

from django.test import TestCase

from teatree.core.models import Ticket
from teatree.loop.dispatch import ActionPayload
from teatree.loop.mechanical import HANDLERS, complete_ticket, ignore_disposed_ticket, reopen_ticket


def _payload(**kwargs: object) -> ActionPayload:
    return cast("ActionPayload", kwargs)


class TestIgnoreDisposedTicket(TestCase):
    def test_transitions_ticket_to_ignored(self) -> None:
        ticket = Ticket.objects.create(overlay="test", issue_url="https://x/1")
        ignore_disposed_ticket(_payload(ticket_id=ticket.pk, reason="duplicate"))
        ticket.refresh_from_db()
        assert ticket.state == "ignored"

    def test_no_op_when_ticket_id_missing(self) -> None:
        ignore_disposed_ticket(_payload(reason="duplicate"))  # should not raise


class TestCompleteTicket(TestCase):
    def test_advances_from_shipped_to_in_review(self) -> None:
        # Direct state injection bypasses the full FSM setup chain.
        Ticket.objects.filter().delete()
        ticket = Ticket.objects.create(overlay="test", issue_url="https://x/1", state="shipped")
        complete_ticket(_payload(ticket_id=ticket.pk))
        ticket.refresh_from_db()
        # The three sequential `if` blocks cascade through review_request → mark_merged
        # → retrospect on the same call.
        assert ticket.state in {"in_review", "merged", "delivered", "retrospected"}

    def test_no_op_when_ticket_not_in_completable_state(self) -> None:
        ticket = Ticket.objects.create(overlay="test", issue_url="https://x/2", state="scoped")
        complete_ticket(_payload(ticket_id=ticket.pk))
        ticket.refresh_from_db()
        assert ticket.state == "scoped"

    def test_no_op_when_ticket_id_missing(self) -> None:
        complete_ticket(_payload())


class TestReopenTicket(TestCase):
    def test_transitions_shipped_ticket_back_to_started(self) -> None:
        ticket = Ticket.objects.create(overlay="test", issue_url="https://x/1", state="shipped")
        reopen_ticket(_payload(ticket_id=ticket.pk, ticket_state="shipped"))
        ticket.refresh_from_db()
        assert ticket.state == "started"

    def test_no_op_when_ticket_id_missing(self) -> None:
        reopen_ticket(_payload(ticket_state="?"))


class TestHandlersRegistry:
    def test_registry_maps_kind_to_handler(self) -> None:
        assert HANDLERS["ticket_disposition"] is ignore_disposed_ticket
        assert HANDLERS["ticket_completion"] is complete_ticket
        assert HANDLERS["ticket_reopen"] is reopen_ticket
