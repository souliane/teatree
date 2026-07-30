"""``ReviewEvidence`` guarded factory + gate-lookup manager (PR-08 / migration M3)."""

import pytest

from teatree.core.models import Ticket
from teatree.core.models.review_evidence import ReviewEvidence, ReviewEvidenceError

_SHA = "a" * 40
_SHA2 = "b" * 40


@pytest.fixture
def ticket(db) -> Ticket:
    return Ticket.objects.create(overlay="t3-teatree", issue_url="https://x/1", repos=["org/a", "org/b"])


class TestRecordValidation:
    def test_records_a_cold_review_row(self, ticket: Ticket) -> None:
        row = ReviewEvidence.record(
            ticket=ticket,
            kind=ReviewEvidence.Kind.COLD_REVIEW,
            reviewer_identity="reviewer-bob",
            verdict="merge_safe",
            head_sha=_SHA,
        )
        assert row.pk is not None
        assert row.kind == ReviewEvidence.Kind.COLD_REVIEW
        assert row.head_sha == _SHA

    def test_records_an_integration_review_covering_two_repos(self, ticket: Ticket) -> None:
        row = ReviewEvidence.record(
            ticket=ticket,
            kind=ReviewEvidence.Kind.INTEGRATION_REVIEW,
            reviewer_identity="reviewer-bob",
            verdict="pass",
            head_sha=_SHA,
            repos=["org/a", "org/b"],
        )
        assert row.kind == ReviewEvidence.Kind.INTEGRATION_REVIEW
        assert set(row.repos) == {"org/a", "org/b"}

    def test_unknown_kind_rejected(self, ticket: Ticket) -> None:
        with pytest.raises(ReviewEvidenceError, match="Unknown kind"):
            ReviewEvidence.record(ticket=ticket, kind="bogus", reviewer_identity="r", verdict="ok", head_sha=_SHA)

    def test_blank_verdict_rejected(self, ticket: Ticket) -> None:
        with pytest.raises(ReviewEvidenceError, match="verdict is required"):
            ReviewEvidence.record(
                ticket=ticket,
                kind=ReviewEvidence.Kind.COLD_REVIEW,
                reviewer_identity="r",
                verdict="   ",
                head_sha=_SHA,
            )

    def test_maker_role_reviewer_rejected(self, ticket: Ticket) -> None:
        with pytest.raises(ReviewEvidenceError, match="maker/coding-agent/loop"):
            ReviewEvidence.record(
                ticket=ticket,
                kind=ReviewEvidence.Kind.COLD_REVIEW,
                reviewer_identity="merge-loop",
                verdict="ok",
                head_sha=_SHA,
            )

    def test_short_sha_rejected(self, ticket: Ticket) -> None:
        with pytest.raises(ReviewEvidenceError, match="full 40-char"):
            ReviewEvidence.record(
                ticket=ticket,
                kind=ReviewEvidence.Kind.COLD_REVIEW,
                reviewer_identity="r",
                verdict="ok",
                head_sha="abc123",
            )

    def test_integration_review_needs_two_repos(self, ticket: Ticket) -> None:
        with pytest.raises(ReviewEvidenceError, match="≥ 2 distinct repos"):
            ReviewEvidence.record(
                ticket=ticket,
                kind=ReviewEvidence.Kind.INTEGRATION_REVIEW,
                reviewer_identity="r",
                verdict="ok",
                head_sha=_SHA,
                repos=["org/a"],
            )

    def test_no_row_written_on_rejection(self, ticket: Ticket) -> None:
        with pytest.raises(ReviewEvidenceError):
            ReviewEvidence.record(ticket=ticket, kind="bogus", reviewer_identity="r", verdict="ok", head_sha=_SHA)
        assert ReviewEvidence.objects.for_ticket(ticket).count() == 0


class TestManagerLookups:
    def test_has_cold_review_false_then_true(self, ticket: Ticket) -> None:
        assert ReviewEvidence.objects.has_cold_review(ticket) is False
        ReviewEvidence.record(
            ticket=ticket,
            kind=ReviewEvidence.Kind.COLD_REVIEW,
            reviewer_identity="r",
            verdict="ok",
            head_sha=_SHA,
        )
        assert ReviewEvidence.objects.has_cold_review(ticket) is True

    def test_integration_review_must_cover_all_repos(self, ticket: Ticket) -> None:
        ReviewEvidence.record(
            ticket=ticket,
            kind=ReviewEvidence.Kind.INTEGRATION_REVIEW,
            reviewer_identity="r",
            verdict="ok",
            head_sha=_SHA,
            repos=["org/a", "org/b"],
        )
        assert ReviewEvidence.objects.has_integration_review_covering(ticket, ["org/a", "org/b"]) is True
        # A review that covered only a subset does not cover a superset changeset.
        assert ReviewEvidence.objects.has_integration_review_covering(ticket, ["org/a", "org/c"]) is False

    def test_partial_integration_review_does_not_cover(self, ticket: Ticket) -> None:
        ReviewEvidence.record(
            ticket=ticket,
            kind=ReviewEvidence.Kind.INTEGRATION_REVIEW,
            reviewer_identity="r",
            verdict="ok",
            head_sha=_SHA2,
            repos=["org/a", "org/b"],
        )
        assert ReviewEvidence.objects.has_integration_review_covering(ticket, ["org/a", "org/b", "org/c"]) is False

    def test_empty_required_repos_trivially_covered(self, ticket: Ticket) -> None:
        assert ReviewEvidence.objects.has_integration_review_covering(ticket, []) is True
