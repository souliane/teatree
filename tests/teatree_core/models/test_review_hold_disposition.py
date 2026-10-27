import pytest
from django.test import TestCase

from teatree.core.models import Finding, ReviewHoldDisposition, ReviewHoldDispositionError, ReviewVerdict

_SHA = "a" * 40


class TestReviewHoldDisposition(TestCase):
    def _pair(self, *, same_reviewer: bool = False) -> tuple[ReviewVerdict, ReviewVerdict]:
        hold = ReviewVerdict.record(
            pr_id=7,
            slug="souliane/teatree",
            reviewed_sha=_SHA,
            verdict="hold",
            reviewer_identity="first-reviewer",
            findings=[
                Finding(
                    severity="major",
                    summary="premise is unproved",
                    file="a.py",
                    line=3,
                    reproduction="pytest test_premise.py: failed",
                )
            ],
        )
        clearing = ReviewVerdict.record(
            pr_id=7,
            slug="souliane/teatree",
            reviewed_sha=_SHA,
            verdict="merge_safe",
            reviewer_identity="first-reviewer" if same_reviewer else "independent-reviewer",
        )
        return hold, clearing

    def test_same_reviewer_cannot_clear_a_hold(self) -> None:
        hold, clearing = self._pair(same_reviewer=True)

        with pytest.raises(ReviewHoldDispositionError, match="independent"):
            ReviewHoldDisposition.record(
                hold=hold,
                clearing_verdict=clearing,
                points=[{"index": 0, "classification": "disproved_premise", "citation": "run at this head"}],
            )

        assert not ReviewHoldDisposition.objects.exists()

    def test_every_point_needs_a_cited_response(self) -> None:
        hold, clearing = self._pair()

        with pytest.raises(ReviewHoldDispositionError, match="unanswered"):
            ReviewHoldDisposition.record(hold=hold, clearing_verdict=clearing, points=[])
        with pytest.raises(ReviewHoldDispositionError, match="citation"):
            ReviewHoldDisposition.record(
                hold=hold,
                clearing_verdict=clearing,
                points=[{"index": 0, "classification": "disproved_premise", "citation": ""}],
            )

        assert not ReviewHoldDisposition.objects.exists()

    def test_legacy_hold_requires_an_answer_for_each_stored_blocker(self) -> None:
        hold, clearing = self._pair()
        hold.findings = [
            {"severity": "major", "kind": "blocking", "summary": "first", "file": "a.py", "line": 3},
            {"severity": "major", "kind": "blocking", "summary": "second", "file": "b.py", "line": 4},
        ]
        hold.save(update_fields=["findings"])
        hold.refresh_from_db()

        assert ReviewHoldDisposition.blocking_indices(hold) == {0, 1}
        with pytest.raises(ReviewHoldDispositionError, match="unanswered"):
            ReviewHoldDisposition.record(
                hold=hold,
                clearing_verdict=clearing,
                points=[{"index": 0, "classification": "disproved_premise", "citation": "new run"}],
            )
