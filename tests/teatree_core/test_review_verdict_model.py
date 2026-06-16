"""``ReviewVerdict`` guarded factory + query/staleness helpers.

The model is the persisted read-side sibling of ``MergeClear``: a recorded
verdict keyed by ``(slug, pr_id, reviewed_sha)`` so a later ``review status``
lookup can answer "safe to approve at the current head?" without re-deriving a
cold review. These cover the issue-time refusals, the structured-findings
round-trip, and the staleness / safe-to-approve logic the status command reads.
"""

import pytest
from django.test import TestCase

from teatree.core.models import Finding, MergeClear, ReviewVerdict, ReviewVerdictError

# ast-grep-ignore: ac-django-no-pytest-django-db
pytestmark = pytest.mark.django_db

_SHA = "a" * 40
_OTHER_SHA = "b" * 40


class TestRecordContract(TestCase):
    def test_records_a_merge_safe_verdict_with_structured_findings(self) -> None:
        verdict = ReviewVerdict.record(
            pr_id=42,
            slug="souliane/teatree",
            reviewed_sha=_SHA,
            verdict="merge_safe",
            reviewer_identity="cold-reviewer",
            findings=[Finding(severity="nit", summary="rename x", file="a.py", line=12)],
        )
        assert verdict.is_merge_safe()
        assert verdict.reviewed_sha == _SHA
        only = verdict.structured_findings[0]
        assert (only.severity, only.summary, only.location()) == ("nit", "rename x", "a.py:12")

    def test_records_a_hold_verdict_on_failed_checks(self) -> None:
        verdict = ReviewVerdict.record(
            pr_id=42,
            slug="souliane/teatree",
            reviewed_sha=_SHA,
            verdict="hold",
            reviewer_identity="cold-reviewer",
            gh_verify_result="failed",
            findings=[Finding(severity="blocker", summary="race window")],
        )
        assert not verdict.is_merge_safe()
        assert verdict.structured_findings[0].location() == "(MR-level)"

    def test_merge_safe_on_non_green_checks_is_refused(self) -> None:
        with pytest.raises(ReviewVerdictError, match="green"):
            ReviewVerdict.record(
                pr_id=1,
                slug="souliane/teatree",
                reviewed_sha=_SHA,
                verdict="merge_safe",
                reviewer_identity="cold-reviewer",
                gh_verify_result="pending",
            )
        assert ReviewVerdict.objects.count() == 0

    def test_abbreviated_sha_is_refused(self) -> None:
        with pytest.raises(ReviewVerdictError, match="40-char"):
            ReviewVerdict.record(
                pr_id=1,
                slug="souliane/teatree",
                reviewed_sha="abc1234",
                verdict="merge_safe",
                reviewer_identity="cold-reviewer",
            )

    def test_empty_reviewer_identity_is_refused(self) -> None:
        with pytest.raises(ReviewVerdictError, match="reviewer_identity"):
            ReviewVerdict.record(
                pr_id=1,
                slug="souliane/teatree",
                reviewed_sha=_SHA,
                verdict="merge_safe",
                reviewer_identity="   ",
            )

    def test_unknown_verdict_is_refused(self) -> None:
        with pytest.raises(ReviewVerdictError, match="verdict"):
            ReviewVerdict.record(
                pr_id=1,
                slug="souliane/teatree",
                reviewed_sha=_SHA,
                verdict="maybe",
                reviewer_identity="cold-reviewer",
            )

    def test_maker_or_loop_reviewer_identity_is_refused(self) -> None:
        # A verdict records an INDEPENDENT cold review; a maker/coding-agent/
        # loop identity is a self-attestation and is refused, mirroring
        # MergeClear.issue (§17.8 clause 3) so the read-side safe-to-approve
        # check can never be satisfied by the author rubber-stamping itself.
        for identity in ("maker", "coding-agent", "merge-loop", "maker:opus", "loop"):
            with (
                self.subTest(identity=identity),
                pytest.raises(ReviewVerdictError, match="maker/coding-agent/loop role"),
            ):
                ReviewVerdict.record(
                    pr_id=1,
                    slug="souliane/teatree",
                    reviewed_sha=_SHA,
                    verdict="merge_safe",
                    reviewer_identity=identity,
                )

    def test_unknown_blast_class_is_refused(self) -> None:
        with pytest.raises(ReviewVerdictError, match="blast_class"):
            ReviewVerdict.record(
                pr_id=1,
                slug="souliane/teatree",
                reviewed_sha=_SHA,
                verdict="hold",
                reviewer_identity="cold-reviewer",
                gh_verify_result="failed",
                blast_class="catastrophic",
            )

    def test_unknown_gh_verify_result_is_refused(self) -> None:
        with pytest.raises(ReviewVerdictError, match="gh_verify_result"):
            ReviewVerdict.record(
                pr_id=1,
                slug="souliane/teatree",
                reviewed_sha=_SHA,
                verdict="hold",
                reviewer_identity="cold-reviewer",
                gh_verify_result="exploded",
            )

    def test_sha_and_slug_are_normalised(self) -> None:
        verdict = ReviewVerdict.record(
            pr_id=1,
            slug="  souliane/teatree  ",
            reviewed_sha=_SHA.upper(),
            verdict="MERGE_SAFE",
            reviewer_identity="cold-reviewer",
        )
        assert verdict.slug == "souliane/teatree"
        assert verdict.reviewed_sha == _SHA
        assert verdict.verdict == ReviewVerdict.Verdict.MERGE_SAFE


class TestQueryHelpers(TestCase):
    def test_latest_for_pr_returns_the_freshest_verdict(self) -> None:
        ReviewVerdict.record(
            pr_id=7,
            slug="souliane/teatree",
            reviewed_sha=_OTHER_SHA,
            verdict="hold",
            reviewer_identity="r1",
            gh_verify_result="failed",
        )
        newest = ReviewVerdict.record(
            pr_id=7,
            slug="souliane/teatree",
            reviewed_sha=_SHA,
            verdict="merge_safe",
            reviewer_identity="r2",
        )
        assert ReviewVerdict.objects.latest_for_pr("souliane/teatree", 7) == newest

    def test_latest_for_pr_is_none_when_nothing_recorded(self) -> None:
        assert ReviewVerdict.objects.latest_for_pr("souliane/teatree", 999) is None

    def test_for_pr_is_scoped_by_slug_and_pr(self) -> None:
        ReviewVerdict.record(
            pr_id=7,
            slug="souliane/teatree",
            reviewed_sha=_SHA,
            verdict="merge_safe",
            reviewer_identity="r",
        )
        ReviewVerdict.record(
            pr_id=7,
            slug="other/repo",
            reviewed_sha=_SHA,
            verdict="merge_safe",
            reviewer_identity="r",
        )
        assert ReviewVerdict.objects.for_pr("souliane/teatree", 7).count() == 1


class TestStalenessAndSafety(TestCase):
    def test_stale_when_head_moved_off_reviewed_sha(self) -> None:
        verdict = ReviewVerdict.record(
            pr_id=1,
            slug="souliane/teatree",
            reviewed_sha=_SHA,
            verdict="merge_safe",
            reviewer_identity="r",
        )
        assert verdict.is_stale_at(_OTHER_SHA)
        assert not verdict.is_stale_at(_SHA.upper())

    def test_safe_to_approve_only_when_at_head_and_checks_green(self) -> None:
        verdict = ReviewVerdict.record(
            pr_id=1,
            slug="souliane/teatree",
            reviewed_sha=_SHA,
            verdict="merge_safe",
            reviewer_identity="r",
        )
        assert verdict.is_safe_to_approve_at(_SHA, live_checks_status="green")
        assert not verdict.is_safe_to_approve_at(_OTHER_SHA, live_checks_status="green")
        assert not verdict.is_safe_to_approve_at(_SHA, live_checks_status="failed")

    def test_hold_verdict_is_never_safe_to_approve(self) -> None:
        verdict = ReviewVerdict.record(
            pr_id=1,
            slug="souliane/teatree",
            reviewed_sha=_SHA,
            verdict="hold",
            reviewer_identity="r",
            gh_verify_result="failed",
        )
        assert not verdict.is_safe_to_approve_at(_SHA, live_checks_status="green")

    def test_blast_class_choices_mirror_merge_clear(self) -> None:
        verdict = ReviewVerdict.record(
            pr_id=1,
            slug="souliane/teatree",
            reviewed_sha=_SHA,
            verdict="merge_safe",
            reviewer_identity="r",
            blast_class="substrate",
        )
        assert verdict.blast_class == MergeClear.BlastClass.SUBSTRATE

    def test_str_summarises_the_verdict(self) -> None:
        verdict = ReviewVerdict.record(
            pr_id=99,
            slug="souliane/teatree",
            reviewed_sha=_SHA,
            verdict="merge_safe",
            reviewer_identity="r",
        )
        assert str(verdict) == f"review-verdict<souliane/teatree#99@{_SHA[:8]} merge_safe>"


class TestFindingRoundTrip(TestCase):
    def test_line_coerced_from_string_digits(self) -> None:
        assert Finding.from_dict({"severity": "nit", "summary": "s", "file": "a.py", "line": "7"}).line == 7

    def test_line_defaults_to_zero_for_non_numeric(self) -> None:
        assert Finding.from_dict({"severity": "nit", "summary": "s", "line": "abc"}).line == 0
        assert Finding.from_dict({"severity": "nit", "summary": "s"}).line == 0

    def test_file_level_location_when_no_line(self) -> None:
        assert Finding(severity="major", summary="s", file="a.py").location() == "a.py"
