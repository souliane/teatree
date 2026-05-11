"""DB-backed tests for ``ActiveTicketsScanner``."""

from django.test import TestCase

from teatree.core.models.ticket import Ticket
from teatree.loop.scanners.active_tickets import ActiveTicketsScanner


class TestActiveTicketsScanner(TestCase):
    def test_emits_signal_for_non_terminal_tickets(self) -> None:
        Ticket.objects.create(overlay="acme", issue_url="https://x/1", state="started")
        Ticket.objects.create(overlay="acme", issue_url="https://x/2", state="delivered")
        signals = ActiveTicketsScanner().scan()
        assert len(signals) == 1
        assert signals[0].kind == "ticket.active"
        assert signals[0].payload["state"] == "started"

    def test_filters_by_overlay_name(self) -> None:
        Ticket.objects.create(overlay="acme", issue_url="https://x/1", state="coded")
        Ticket.objects.create(overlay="other", issue_url="https://x/2", state="coded")
        signals = ActiveTicketsScanner(overlay_name="acme").scan()
        assert len(signals) == 1
        assert signals[0].payload["ticket_number"] == "1"

    def test_excludes_ignored_tickets(self) -> None:
        Ticket.objects.create(overlay="acme", issue_url="https://x/1", state="ignored")
        assert ActiveTicketsScanner().scan() == []
