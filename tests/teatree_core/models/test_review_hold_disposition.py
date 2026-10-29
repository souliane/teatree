import pytest
from django.test import TestCase

from teatree.core.models import Finding, ReviewHoldDisposition, ReviewHoldDispositionError, ReviewVerdict

_SHA = "a" * 40
_COUNTER = {"command": "pytest test_premise.py::test_premise", "observed_result": "1 passed in 0.1s at this head"}


class TestReviewHoldDisposition(TestCase):
    def _pair(
        self, *, same_reviewer: bool = False, finding: Finding | None = None
    ) -> tuple[ReviewVerdict, ReviewVerdict]:
        hold = ReviewVerdict.record(
            pr_id=7,
            slug="souliane/teatree",
            reviewed_sha=_SHA,
            verdict="hold",
            reviewer_identity="first-reviewer",
            findings=[
                finding
                or Finding(
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

    def test_unstructured_hold_cannot_be_answered_as_a_venue_limit(self) -> None:
        hold, clearing = self._pair()
        hold.findings = []
        hold.save(update_fields=["findings"])

        with pytest.raises(ReviewHoldDispositionError, match="venue_limit"):
            ReviewHoldDisposition.record(
                hold=hold,
                clearing_verdict=clearing,
                points=[{"index": -1, "classification": "venue_limit", "citation": "retry"}],
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

    def test_a_one_character_citation_cannot_answer_an_executed_reproduction(self) -> None:
        hold, clearing = self._pair()

        with pytest.raises(ReviewHoldDispositionError, match="needs a counter_reproduction"):
            ReviewHoldDisposition.record(
                hold=hold,
                clearing_verdict=clearing,
                points=[{"index": 0, "classification": "disproved_premise", "citation": "x"}],
            )

        assert not ReviewHoldDisposition.objects.exists()
        assert ReviewVerdict.objects.unreconciled_holds_at(slug=hold.slug, pr_id=7, head_sha=_SHA) == [hold]

    def test_a_thin_or_repeated_counter_reproduction_is_refused(self) -> None:
        hold, clearing = self._pair()
        thin = [
            {"command": "x", "observed_result": "1 passed in 0.1s at this head"},
            {"command": "pytest test_premise.py::test_premise", "observed_result": "ok"},
            {"command": "", "observed_result": ""},
            {"command": "pytest test_premise.py: failed", "observed_result": "1 passed in 0.1s at this head"},
            {"command": "pytest test_premise.py::test_premise", "observed_result": "pytest test_premise.py: failed"},
            {
                "command": "pytest test_premise.py::test_premise",
                "observed_result": "pytest test_premise.py::test_premise",
            },
            "ran it",
        ]

        for counter in thin:
            with self.subTest(counter=counter), pytest.raises(ReviewHoldDispositionError, match="counter_reproduction"):
                ReviewHoldDisposition.record(
                    hold=hold,
                    clearing_verdict=clearing,
                    points=[
                        {
                            "index": 0,
                            "classification": "disproved_premise",
                            "citation": "run at this head",
                            "counter_reproduction": counter,
                        }
                    ],
                )

        assert not ReviewHoldDisposition.objects.exists()

    def test_a_counter_reproduction_with_a_command_and_a_result_answers_the_point(self) -> None:
        hold, clearing = self._pair()

        disposition = ReviewHoldDisposition.record(
            hold=hold,
            clearing_verdict=clearing,
            points=[
                {
                    "index": 0,
                    "classification": "disproved_premise",
                    "citation": "run at this head",
                    "counter_reproduction": _COUNTER,
                }
            ],
        )

        assert disposition.covers(hold)

    def test_an_executed_basis_without_a_recorded_reproduction_still_needs_a_counter_reproduction(self) -> None:
        hold, clearing = self._pair(
            finding=Finding(severity="major", summary="fails when run", file="a.py", line=3, basis="executed")
        )

        with pytest.raises(ReviewHoldDispositionError, match="needs a counter_reproduction"):
            ReviewHoldDisposition.record(
                hold=hold,
                clearing_verdict=clearing,
                points=[{"index": 0, "classification": "disproved_premise", "citation": "looks fine"}],
            )

    def test_a_point_without_a_reproduction_is_answered_by_a_cited_response(self) -> None:
        hold, clearing = self._pair(
            finding=Finding(severity="major", summary="behaviour no test pins", file="a.py", line=3)
        )

        disposition = ReviewHoldDisposition.record(
            hold=hold,
            clearing_verdict=clearing,
            points=[{"index": 0, "classification": "disproved_premise", "citation": "pinned by test_x at this head"}],
        )

        assert disposition.covers(hold)

    def test_a_clearing_verdict_from_another_head_or_pr_cannot_clear_a_hold(self) -> None:
        hold, _same_head = self._pair()
        points = [
            {
                "index": 0,
                "classification": "disproved_premise",
                "citation": "run at this head",
                "counter_reproduction": _COUNTER,
            }
        ]
        elsewhere = {"reviewed_sha": "b" * 40, "pr_id": 7}, {"reviewed_sha": _SHA, "pr_id": 8}

        for where in elsewhere:
            clearing = ReviewVerdict.record(
                slug="souliane/teatree", verdict="merge_safe", reviewer_identity="another-reviewer", **where
            )
            with self.subTest(where=where), pytest.raises(ReviewHoldDispositionError, match="same PR and head"):
                ReviewHoldDisposition.record(hold=hold, clearing_verdict=clearing, points=points)

        assert not ReviewHoldDisposition.objects.exists()

    def test_a_confirmed_failed_check_cannot_be_dispositioned(self) -> None:
        hold = ReviewVerdict.record(
            pr_id=7,
            slug="souliane/teatree",
            reviewed_sha=_SHA,
            verdict="hold",
            reviewer_identity="first-reviewer",
            gh_verify_result="failed",
            findings=[Finding(severity="major", summary="required check is red", reproduction="gh run view: failure")],
        )
        clearing = ReviewVerdict.record(
            pr_id=7,
            slug="souliane/teatree",
            reviewed_sha=_SHA,
            verdict="merge_safe",
            reviewer_identity="independent-reviewer",
        )

        with pytest.raises(ReviewHoldDispositionError, match="confirmed failed check"):
            ReviewHoldDisposition.record(
                hold=hold,
                clearing_verdict=clearing,
                points=[
                    {
                        "index": 0,
                        "classification": "disproved_premise",
                        "citation": "re-ran the check at this head",
                        "counter_reproduction": _COUNTER,
                    }
                ],
            )

        assert not ReviewHoldDisposition.objects.exists()
