"""The persisted, indexed ``issue_number`` denormalization (burndown Unit 5 3e#8).

``_ticket_by_number`` used to load every non-synthetic ``Ticket`` and filter the
DERIVED ``ticket_number`` in Python — O(all tickets) per worktree resolve, no
index. The forge issue number is now denormalized into a real indexed
``issue_number`` column (populated on ``save`` + backfilled by migration), so the
resolve is a single indexed lookup. The ``ticket_number`` property (57 consumers)
is unchanged: it composes ``issue_number`` derivation with the ``str(pk)`` fallback.
"""

from django.db import connection
from django.test import TestCase
from django.test.utils import CaptureQueriesContext

from teatree.core.intake.resolve import _ticket_by_number
from teatree.core.models import Ticket
from teatree.core.models.ticket_number import derive_issue_number


class TestDeriveIssueNumber:
    def test_trailing_number(self) -> None:
        assert derive_issue_number("https://github.com/example/repo/issues/123") == "123"

    def test_empty_url(self) -> None:
        assert derive_issue_number("") == ""

    def test_no_trailing_number(self) -> None:
        assert derive_issue_number("https://example.com/no-number") == ""

    def test_trailing_zero_is_not_a_real_issue(self) -> None:
        assert derive_issue_number("https://github.com/example/repo/issues/0") == ""


class TestIssueNumberColumn(TestCase):
    def test_field_is_indexed(self) -> None:
        field = Ticket._meta.get_field("issue_number")
        assert field.db_index is True

    def test_populated_from_issue_url_on_save(self) -> None:
        ticket = Ticket.objects.create(issue_url="https://github.com/example/repo/issues/466")
        ticket.refresh_from_db()
        assert ticket.issue_number == "466"

    def test_blank_when_url_has_no_trailing_number(self) -> None:
        ticket = Ticket.objects.create(issue_url="https://example.com/no-number")
        ticket.refresh_from_db()
        assert ticket.issue_number == ""

    def test_recomputed_when_issue_url_changes(self) -> None:
        ticket = Ticket.objects.create(issue_url="https://github.com/example/repo/issues/1")
        ticket.issue_url = "https://github.com/example/repo/issues/2"
        ticket.save()
        ticket.refresh_from_db()
        assert ticket.issue_number == "2"

    def test_ticket_number_property_still_falls_back_to_pk(self) -> None:
        # The property surface (57 consumers) is unchanged: no forge number → pk.
        ticket = Ticket.objects.create(issue_url="https://example.com/no-number")
        assert ticket.ticket_number == str(ticket.pk)


class TestTicketByNumberUsesIndexedColumn(TestCase):
    def test_resolve_queries_the_indexed_column_not_a_full_python_scan(self) -> None:
        Ticket.objects.create(issue_url="https://github.com/example/repo/issues/466")
        with CaptureQueriesContext(connection) as captured:
            _ticket_by_number("466")
        sql = " ".join(q["sql"] for q in captured.captured_queries)
        # The pre-fix code never filtered on a number column (it scanned in Python);
        # the fix pushes the match into the DB WHERE clause on the indexed column.
        assert "issue_number" in sql

    def test_resolves_forge_number_ticket(self) -> None:
        ticket = Ticket.objects.create(issue_url="https://a.example.com/x/issues/7")
        assert _ticket_by_number("7") == ticket

    def test_pk_fallback_ticket_still_resolves(self) -> None:
        # A ticket whose issue_url carries no forge number resolves by its pk-derived
        # ticket_number — the branch of the old Python filter the index must preserve.
        ticket = Ticket.objects.create(issue_url="https://example.com/no-number")
        assert _ticket_by_number(str(ticket.pk)) == ticket

    def test_returns_none_when_no_match(self) -> None:
        Ticket.objects.create(issue_url="https://a.example.com/x/issues/42")
        assert _ticket_by_number("5") is None

    def test_synthetic_auto_rows_are_never_resolved(self) -> None:
        Ticket.objects.create(issue_url="auto:5-some-branch")
        assert _ticket_by_number("5") is None
