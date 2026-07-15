"""Durable issue-implementer ledger tests (#1549).

The marker dedupes re-ticks (idempotent ``claim`` keyed on
``(issue_url, overlay)``) and exposes the max-concurrent budget the loop
reads (``in_flight_count`` over non-terminal rows). FK to ``Ticket`` is
``SET_NULL`` so deleting a ticket orphans the marker without losing the
dedup record.
"""

from django.test import TestCase

from teatree.core.models import ImplementedIssueMarker
from teatree.instance_id import instance_id
from tests.factories import ImplementedIssueMarkerFactory, TicketFactory


class TestClaim(TestCase):
    def test_inserts_first_observation(self) -> None:
        row = ImplementedIssueMarker.objects.claim(
            "https://github.com/o/r/issues/1",
            "acme",
            head_sha="abc123",
        )

        assert row is not None
        assert row.issue_url == "https://github.com/o/r/issues/1"
        assert row.overlay == "acme"
        assert row.head_sha == "abc123"
        assert row.state == ImplementedIssueMarker.State.DISPATCHED

    def test_returns_none_on_duplicate(self) -> None:
        ImplementedIssueMarker.objects.claim("https://github.com/o/r/issues/2", "acme")
        again = ImplementedIssueMarker.objects.claim("https://github.com/o/r/issues/2", "acme")
        assert again is None

    def test_distinct_overlay_is_not_a_duplicate(self) -> None:
        first = ImplementedIssueMarker.objects.claim("https://github.com/o/r/issues/3", "acme")
        other = ImplementedIssueMarker.objects.claim("https://github.com/o/r/issues/3", "widgets")
        assert first is not None
        assert other is not None
        assert first.pk != other.pk

    def test_no_op_on_missing_url(self) -> None:
        assert ImplementedIssueMarker.objects.claim("", "acme") is None

    def test_stamps_the_claiming_instance_id(self) -> None:
        row = ImplementedIssueMarker.objects.claim("https://github.com/o/r/issues/7", "acme")
        assert row is not None
        assert row.claimed_by_instance == instance_id()

    def test_explicit_instance_overrides_the_default(self) -> None:
        row = ImplementedIssueMarker.objects.claim(
            "https://github.com/o/r/issues/8",
            "acme",
            claimed_by_instance="other-box",
        )
        assert row is not None
        assert row.claimed_by_instance == "other-box"


class TestInFlightCount(TestCase):
    def test_counts_dispatched_and_ticket_created_excludes_terminal(self) -> None:
        ImplementedIssueMarkerFactory(overlay="acme")
        ImplementedIssueMarkerFactory(overlay="acme", ticket_created=True)
        ImplementedIssueMarkerFactory(overlay="acme", abandoned=True)
        ImplementedIssueMarkerFactory(overlay="acme", completed=True)

        assert ImplementedIssueMarker.objects.in_flight_count("acme") == 2

    def test_scoped_per_overlay(self) -> None:
        ImplementedIssueMarkerFactory(overlay="acme")
        ImplementedIssueMarkerFactory(overlay="widgets")

        assert ImplementedIssueMarker.objects.in_flight_count("acme") == 1


class TestTicketRelation(TestCase):
    def test_ticket_delete_sets_null(self) -> None:
        ticket = TicketFactory()
        marker = ImplementedIssueMarkerFactory(overlay="acme", ticket=ticket)

        ticket.delete()
        marker.refresh_from_db()

        assert marker.ticket_id is None


class TestStr(TestCase):
    def test_renders_url_and_state(self) -> None:
        marker = ImplementedIssueMarkerFactory(issue_url="https://github.com/o/r/issues/9", ticket_created=True)
        rendered = str(marker)
        assert "impl-issue" in rendered
        assert "https://github.com/o/r/issues/9" in rendered
        assert "ticket_created" in rendered
