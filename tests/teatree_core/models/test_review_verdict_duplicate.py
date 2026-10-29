from django.test import TestCase

from teatree.core.models.review_verdict import ReviewVerdict


class TestDuplicateVerdictRefresh(TestCase):
    def test_later_unverified_attempt_replaces_stale_evidence_gap(self) -> None:
        first = ReviewVerdict.record(
            pr_id=42,
            slug="souliane/teatree",
            reviewed_sha="a" * 40,
            verdict="unverified",
            reviewer_identity="cold-reviewer",
            gh_verify_result="pending",
            evidence_gaps=["ci_unreadable"],
        )

        second = ReviewVerdict.record(
            pr_id=42,
            slug="souliane/teatree",
            reviewed_sha="a" * 40,
            verdict="unverified",
            reviewer_identity="cold-reviewer",
            gh_verify_result="green",
            evidence_gaps=["tests_unavailable"],
        )

        assert second.pk == first.pk
        assert second.evidence_gaps == ["tests_unavailable"]
        assert second.gh_verify_result == "green"
