"""``Ticket.context`` — append-only durable per-ticket knowledge store (#627)."""

import re

from django.db import connection
from django.test import TestCase

from teatree.core.models import Ticket


class TicketContextFieldTest(TestCase):
    def test_context_defaults_to_empty_string(self) -> None:
        ticket = Ticket.objects.create(overlay="test", issue_url="https://example.com/issues/1")
        assert ticket.context == ""

    def test_context_persists_free_text(self) -> None:
        ticket = Ticket.objects.create(overlay="test", issue_url="https://example.com/issues/2")
        ticket.context = "dev_lr_id = 5842"
        ticket.save()
        ticket.refresh_from_db()
        assert ticket.context == "dev_lr_id = 5842"

    def test_context_column_present_in_db_schema(self) -> None:
        """The migration is applied — the column exists on the table."""
        with connection.cursor() as cursor:
            columns = {col.name for col in connection.introspection.get_table_description(cursor, "teatree_ticket")}
        assert "context" in columns


class TicketContextAppendTest(TestCase):
    def test_append_context_prefixes_timestamp_block(self) -> None:
        ticket = Ticket.objects.create(overlay="test", issue_url="https://example.com/issues/3")
        ticket.append_context("dev_lr_id = 5842")
        ticket.refresh_from_db()
        assert re.fullmatch(
            r"\n\n\[\d{4}-\d{2}-\d{2} \d{2}:\d{2}\] dev_lr_id = 5842",
            ticket.context,
        )

    def test_append_context_is_additive_and_ordered(self) -> None:
        ticket = Ticket.objects.create(overlay="test", issue_url="https://example.com/issues/4")
        ticket.append_context("first")
        ticket.append_context("second")
        ticket.refresh_from_db()
        first_at = ticket.context.index("first")
        second_at = ticket.context.index("second")
        assert first_at < second_at
        assert ticket.context.count("[") == 2

    def test_append_context_rejects_blank(self) -> None:
        ticket = Ticket.objects.create(overlay="test", issue_url="https://example.com/issues/5")
        with self.assertRaises(ValueError):  # noqa: PT027 — TestCase convention in this module.
            ticket.append_context("   ")
