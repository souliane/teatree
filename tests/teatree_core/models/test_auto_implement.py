"""``teatree.core.models.auto_implement`` — the plan-skipped direct-coding marker (#10)."""

from django.test import TestCase

from teatree.core.models import Ticket
from teatree.core.models.auto_implement import is_auto_implement, mark_auto_implement


class TestAutoImplementMarker(TestCase):
    def test_unmarked_ticket_is_not_auto_implement(self) -> None:
        ticket = Ticket.objects.create(overlay="test", role=Ticket.Role.AUTHOR)
        assert is_auto_implement(ticket) is False

    def test_marked_ticket_reads_back_true(self) -> None:
        ticket = Ticket.objects.create(overlay="test", role=Ticket.Role.AUTHOR)
        mark_auto_implement(ticket)
        ticket.refresh_from_db()
        assert is_auto_implement(ticket) is True

    def test_marker_survives_a_concurrent_extra_writer(self) -> None:
        # merge_extra is the canonical locked RMW — a sibling key must survive.
        ticket = Ticket.objects.create(overlay="test", role=Ticket.Role.AUTHOR)
        ticket.merge_extra(set_keys={"description": "hello"})
        mark_auto_implement(ticket)
        ticket.refresh_from_db()
        assert is_auto_implement(ticket) is True
        assert ticket.extra["description"] == "hello"

    def test_malformed_extra_reads_as_not_auto_implement(self) -> None:
        # Fail-safe: a non-dict extra can never widen the code_direct gate.
        ticket = Ticket(overlay="test", role=Ticket.Role.AUTHOR)
        ticket.extra = None
        assert is_auto_implement(ticket) is False
