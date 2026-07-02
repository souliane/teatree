"""`t3 ticket context show|add|edit` — durable per-ticket knowledge store CLI (#627, #2293)."""

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
            "repo_namespaced_key": "",
            "context": "\n\n[2026-05-18 09:00] dev_lr_id = 5842",
        }

    def test_show_unknown_ticket_exits_nonzero(self) -> None:
        with pytest.raises(SystemExit):
            call_command("ticket", "context", "show", "999999")

    def test_show_resolves_by_full_ticket_url(self) -> None:
        ticket = Ticket.objects.create(
            overlay="test",
            issue_url="https://github.com/acme-eng/widgets/issues/42",
            context="notes",
        )
        result = cast(
            "dict[str, object]",
            call_command("ticket", "context", "show", "https://github.com/acme-eng/widgets/issues/42"),
        )
        assert result == {
            "ticket_id": int(ticket.pk),
            "repo_namespaced_key": "acme-eng/widgets#42",
            "context": "notes",
        }

    def test_show_resolves_by_repo_namespaced_key(self) -> None:
        ticket = Ticket.objects.create(
            overlay="test",
            issue_url="https://github.com/acme-eng/widgets/issues/42",
            context="notes",
        )
        result = cast(
            "dict[str, object]",
            call_command("ticket", "context", "show", "acme-eng/widgets#42"),
        )
        assert result["ticket_id"] == int(ticket.pk)

    def test_show_never_confuses_two_repos_sharing_an_issue_number(self) -> None:
        """The #2293 regression.

        Two repos with the same bare IID must resolve to their own
        ticket's context, never the sibling's.
        """
        bugs = Ticket.objects.create(
            overlay="test",
            issue_url="https://github.com/acme-eng/bugs/issues/2242",
            context="bugs-repo notes",
        )
        product = Ticket.objects.create(
            overlay="test",
            issue_url="https://github.com/acme-product/repo/issues/2242",
            context="product-repo notes",
        )

        bugs_result = cast(
            "dict[str, object]",
            call_command("ticket", "context", "show", "acme-eng/bugs#2242"),
        )
        product_result = cast(
            "dict[str, object]",
            call_command("ticket", "context", "show", "acme-product/repo#2242"),
        )

        assert bugs_result == {
            "ticket_id": int(bugs.pk),
            "repo_namespaced_key": "acme-eng/bugs#2242",
            "context": "bugs-repo notes",
        }
        assert product_result == {
            "ticket_id": int(product.pk),
            "repo_namespaced_key": "acme-product/repo#2242",
            "context": "product-repo notes",
        }


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

    def test_add_resolves_by_repo_namespaced_key(self) -> None:
        ticket = Ticket.objects.create(overlay="test", issue_url="https://github.com/acme-eng/widgets/issues/42")
        result = cast(
            "dict[str, object]",
            call_command("ticket", "context", "add", "acme-eng/widgets#42", "dev_lr_id: 5842"),
        )
        ticket.refresh_from_db()
        assert "dev_lr_id: 5842" in ticket.context
        assert result["ticket_id"] == int(ticket.pk)


class TicketContextEditTest(TestCase):
    def test_edit_replaces_full_field_via_editor(self) -> None:
        ticket = Ticket.objects.create(
            overlay="test",
            issue_url="https://example.com/issues/4",
            context="old",
        )
        with patch("teatree.core.management.commands._context_commands.click.edit", return_value="new full body"):
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
        with patch("teatree.core.management.commands._context_commands.click.edit", return_value=None):
            call_command("ticket", "context", "edit", str(ticket.pk))
        ticket.refresh_from_db()
        assert ticket.context == "keep me"

    def test_edit_unknown_ticket_exits_nonzero(self) -> None:
        with pytest.raises(SystemExit):
            call_command("ticket", "context", "edit", "999999")
