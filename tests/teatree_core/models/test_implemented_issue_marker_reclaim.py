"""An ABANDONED issue marker is re-claimable (F10).

Before the fix ``claim()`` was ``get_or_create → None`` for ANY existing row, and
ABANDONED markers are never deleted — so an issue whose first attempt was abandoned kept
its marker forever and intake skipped it permanently. ``claim()`` now re-claims an
ABANDONED row via a state CAS (fresh DISPATCHED claim), while a live or COMPLETED row
still returns ``None``.
"""

from django.test import TestCase

from teatree.core.models import ImplementedIssueMarker
from teatree.core.models.implemented_issue_marker import MarkerClaimFields
from teatree.instance_id import instance_id
from tests.factories import ImplementedIssueMarkerFactory, TicketFactory


class TestReclaimAbandonedMarker(TestCase):
    def test_abandoned_marker_is_reclaimed_as_fresh_dispatch(self) -> None:
        url = "https://github.com/o/r/issues/501"
        marker = ImplementedIssueMarkerFactory(
            issue_url=url,
            overlay="acme",
            abandoned=True,
            head_sha="deadbeef",
            claim_ref_sha="oldsha",
            ticket=TicketFactory(),
        )

        reclaimed = ImplementedIssueMarker.objects.claim(url, "acme")

        assert reclaimed is not None
        assert reclaimed.pk == marker.pk  # same row (unique on issue_url/overlay), re-claimed in place
        assert reclaimed.state == ImplementedIssueMarker.State.DISPATCHED
        assert reclaimed.claimed_by_instance == instance_id()
        assert reclaimed.head_sha == ""  # stale dispatch artifacts cleared for the fresh attempt
        assert reclaimed.claim_ref_sha == ""
        assert reclaimed.ticket_id is None

    def test_live_marker_is_not_reclaimed(self) -> None:
        url = "https://github.com/o/r/issues/502"
        ImplementedIssueMarkerFactory(issue_url=url, overlay="acme")  # DISPATCHED (live)
        assert ImplementedIssueMarker.objects.claim(url, "acme") is None

    def test_completed_marker_is_not_reclaimed(self) -> None:
        # A shipped/merged issue must never be re-implemented.
        url = "https://github.com/o/r/issues/503"
        ImplementedIssueMarkerFactory(issue_url=url, overlay="acme", completed=True)
        assert ImplementedIssueMarker.objects.claim(url, "acme") is None

    def test_claim_fields_land_on_the_fresh_marker(self) -> None:
        # The per-claim overrides typed by `MarkerClaimFields` are written onto the
        # newly-created marker row.
        url = "https://github.com/o/r/issues/510"
        fields = MarkerClaimFields(head_sha="cafebabe", claim_ref_sha="refs/heads/probe")
        marker = ImplementedIssueMarker.objects.claim(url, "acme", **fields)
        assert marker is not None
        assert marker.head_sha == "cafebabe"
        assert marker.claim_ref_sha == "refs/heads/probe"

    def test_reclaim_is_single_winner(self) -> None:
        # The CAS admits exactly one re-claimer of an ABANDONED row; the second sees the
        # row already DISPATCHED and gets None (no double-dispatch of the re-claimed issue).
        url = "https://github.com/o/r/issues/504"
        ImplementedIssueMarkerFactory(issue_url=url, overlay="acme", abandoned=True)
        first = ImplementedIssueMarker.objects.claim(url, "acme")
        second = ImplementedIssueMarker.objects.claim(url, "acme")
        assert first is not None
        assert second is None
