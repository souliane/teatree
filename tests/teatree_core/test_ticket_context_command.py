"""`t3 ticket context show|add|edit` — durable per-ticket knowledge store CLI (#627)."""

from typing import cast
from unittest.mock import patch

import pytest
from django.core.management import call_command
from django.test import TestCase

from teatree.core.models import Ticket


class TicketContextShowTest(TestCase):
    def test_show_returns_context(self) -> None:
        ticket = Ticket.objects.create(
            overlay="test",
            issue_url="https://example.com/issues/1",
            context="\n\n[2026-05-18 09:00] dev_lr_id = 5842",
        )
        result = cast(
            "dict[str, object]",
            call_command("ticket", "context", "show", str(ticket.pk)),
        )
        assert result == {
            "ticket_id": int(ticket.pk),
            "context": "\n\n[2026-05-18 09:00] dev_lr_id = 5842",
        }

    def test_show_unknown_ticket_exits_nonzero(self) -> None:
        with pytest.raises(SystemExit):
            call_command("ticket", "context", "show", "999999")


class TicketContextAddTest(TestCase):
    def test_add_appends_timestamped_block(self) -> None:
        ticket = Ticket.objects.create(overlay="test", issue_url="https://example.com/issues/2")
        result = cast(
            "dict[str, object]",
            call_command("ticket", "context", "add", str(ticket.pk), "dev_lr_id: 5842"),
        )
        ticket.refresh_from_db()
        assert "dev_lr_id: 5842" in ticket.context
        assert ticket.context.startswith("\n\n[")
        assert result["ticket_id"] == int(ticket.pk)

    def test_add_unknown_ticket_exits_nonzero(self) -> None:
        with pytest.raises(SystemExit):
            call_command("ticket", "context", "add", "999999", "x: y")

    def test_add_blank_entry_exits_nonzero(self) -> None:
        ticket = Ticket.objects.create(overlay="test", issue_url="https://example.com/issues/3")
        with pytest.raises(SystemExit):
            call_command("ticket", "context", "add", str(ticket.pk), "   ")


class TicketContextEditTest(TestCase):
    def test_edit_replaces_full_field_via_editor(self) -> None:
        ticket = Ticket.objects.create(
            overlay="test",
            issue_url="https://example.com/issues/4",
            context="old",
        )
        with patch("teatree.core.management.commands.ticket.click.edit", return_value="new full body"):
            result = cast(
                "dict[str, object]",
                call_command("ticket", "context", "edit", str(ticket.pk)),
            )
        ticket.refresh_from_db()
        assert ticket.context == "new full body"
        assert result["ticket_id"] == int(ticket.pk)

    def test_edit_aborted_leaves_context_untouched(self) -> None:
        ticket = Ticket.objects.create(
            overlay="test",
            issue_url="https://example.com/issues/5",
            context="keep me",
        )
        with patch("teatree.core.management.commands.ticket.click.edit", return_value=None):
            call_command("ticket", "context", "edit", str(ticket.pk))
        ticket.refresh_from_db()
        assert ticket.context == "keep me"

    def test_edit_unknown_ticket_exits_nonzero(self) -> None:
        with pytest.raises(SystemExit):
            call_command("ticket", "context", "edit", "999999")
