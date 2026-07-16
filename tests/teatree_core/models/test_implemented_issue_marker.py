"""Durable issue-implementer ledger tests (#1549).

The marker dedupes re-ticks (idempotent ``claim`` keyed on
``(issue_url, overlay)``) and exposes the max-concurrent budget the loop
reads (``in_flight_count`` over non-terminal rows). FK to ``Ticket`` is
``SET_NULL`` so deleting a ticket orphans the marker without losing the
dedup record.
"""

from datetime import timedelta

from django.test import TestCase
from django.utils import timezone

from teatree.core.models import ImplementedIssueMarker, Ticket
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


class TestReconcileStale(TestCase):
    """#3275 — retroactively free markers whose ticket went terminal/gone.

    The release-on-completion signal only fires on the LIVE transition event;
    a marker orphaned while the pipeline was down never leaves ``dispatched``
    and permanently exhausts the in-flight budget. ``reconcile_stale`` is the
    retroactive path that heals it.
    """

    def _terminal_ticket(self, issue_url: str):
        return TicketFactory(overlay="acme", issue_url=issue_url, state=Ticket.State.MERGED)

    def test_releases_dispatched_marker_with_merged_ticket(self) -> None:
        url = "https://github.com/o/r/issues/100"
        self._terminal_ticket(url)
        marker = ImplementedIssueMarkerFactory(overlay="acme", issue_url=url)  # DISPATCHED

        result = ImplementedIssueMarker.objects.reconcile_stale("acme")

        marker.refresh_from_db()
        assert marker.state == ImplementedIssueMarker.State.COMPLETED
        assert result.completed == (marker.pk,)
        assert result.released == 1

    def test_frees_the_in_flight_budget(self) -> None:
        url = "https://github.com/o/r/issues/101"
        self._terminal_ticket(url)
        ImplementedIssueMarkerFactory(overlay="acme", issue_url=url)
        assert ImplementedIssueMarker.objects.in_flight_count("acme") == 1

        ImplementedIssueMarker.objects.reconcile_stale("acme")

        assert ImplementedIssueMarker.objects.in_flight_count("acme") == 0

    def test_keeps_marker_whose_ticket_is_still_live(self) -> None:
        url = "https://github.com/o/r/issues/102"
        TicketFactory(overlay="acme", issue_url=url, state=Ticket.State.CODED)
        marker = ImplementedIssueMarkerFactory(overlay="acme", issue_url=url)

        result = ImplementedIssueMarker.objects.reconcile_stale("acme")

        marker.refresh_from_db()
        assert marker.state == ImplementedIssueMarker.State.DISPATCHED
        assert result.released == 0

    def test_releases_ticket_created_marker_with_delivered_ticket(self) -> None:
        url = "https://github.com/o/r/issues/103"
        TicketFactory(overlay="acme", issue_url=url, state=Ticket.State.DELIVERED)
        marker = ImplementedIssueMarkerFactory(overlay="acme", issue_url=url, ticket_created=True)

        ImplementedIssueMarker.objects.reconcile_stale("acme")

        marker.refresh_from_db()
        assert marker.state == ImplementedIssueMarker.State.COMPLETED

    def test_abandons_orphan_with_gone_ticket_past_grace(self) -> None:
        marker = ImplementedIssueMarkerFactory(overlay="acme", issue_url="https://github.com/o/r/issues/104")
        ImplementedIssueMarker.objects.filter(pk=marker.pk).update(dispatched_at=timezone.now() - timedelta(hours=48))

        result = ImplementedIssueMarker.objects.reconcile_stale("acme")

        marker.refresh_from_db()
        assert marker.state == ImplementedIssueMarker.State.ABANDONED
        assert result.abandoned == (marker.pk,)

    def test_keeps_fresh_orphan_within_grace(self) -> None:
        marker = ImplementedIssueMarkerFactory(overlay="acme", issue_url="https://github.com/o/r/issues/105")

        result = ImplementedIssueMarker.objects.reconcile_stale("acme")

        marker.refresh_from_db()
        assert marker.state == ImplementedIssueMarker.State.DISPATCHED
        assert result.released == 0

    def test_leaves_already_terminal_markers_untouched(self) -> None:
        url = "https://github.com/o/r/issues/106"
        self._terminal_ticket(url)
        completed = ImplementedIssueMarkerFactory(overlay="acme", issue_url=url, completed=True)
        abandoned = ImplementedIssueMarkerFactory(
            overlay="acme", issue_url="https://github.com/o/r/issues/107", abandoned=True
        )

        result = ImplementedIssueMarker.objects.reconcile_stale("acme")

        completed.refresh_from_db()
        abandoned.refresh_from_db()
        assert completed.state == ImplementedIssueMarker.State.COMPLETED
        assert abandoned.state == ImplementedIssueMarker.State.ABANDONED
        assert result.released == 0

    def test_scoped_to_overlay(self) -> None:
        url_a = "https://github.com/o/r/issues/108"
        url_b = "https://github.com/o/r/issues/109"
        self._terminal_ticket(url_a)
        TicketFactory(overlay="widgets", issue_url=url_b, state=Ticket.State.MERGED)
        acme_marker = ImplementedIssueMarkerFactory(overlay="acme", issue_url=url_a)
        widgets_marker = ImplementedIssueMarkerFactory(overlay="widgets", issue_url=url_b)

        ImplementedIssueMarker.objects.reconcile_stale("acme")

        acme_marker.refresh_from_db()
        widgets_marker.refresh_from_db()
        assert acme_marker.state == ImplementedIssueMarker.State.COMPLETED
        assert widgets_marker.state == ImplementedIssueMarker.State.DISPATCHED

    def test_empty_overlay_reconciles_every_overlay(self) -> None:
        url_a = "https://github.com/o/r/issues/110"
        url_b = "https://github.com/o/r/issues/111"
        self._terminal_ticket(url_a)
        TicketFactory(overlay="widgets", issue_url=url_b, state=Ticket.State.MERGED)
        acme_marker = ImplementedIssueMarkerFactory(overlay="acme", issue_url=url_a)
        widgets_marker = ImplementedIssueMarkerFactory(overlay="widgets", issue_url=url_b)

        result = ImplementedIssueMarker.objects.reconcile_stale()

        acme_marker.refresh_from_db()
        widgets_marker.refresh_from_db()
        assert acme_marker.state == ImplementedIssueMarker.State.COMPLETED
        assert widgets_marker.state == ImplementedIssueMarker.State.COMPLETED
        assert result.released == 2

    def test_find_stale_previews_without_mutating(self) -> None:
        url = "https://github.com/o/r/issues/112"
        self._terminal_ticket(url)
        marker = ImplementedIssueMarkerFactory(overlay="acme", issue_url=url)

        result = ImplementedIssueMarker.objects.find_stale("acme")

        marker.refresh_from_db()
        assert marker.state == ImplementedIssueMarker.State.DISPATCHED
        assert result.completed == (marker.pk,)
