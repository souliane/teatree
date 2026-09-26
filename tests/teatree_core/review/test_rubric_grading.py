"""The shared rubric-grades validator both the envelope recorder and the CLI run (#4832).

Pure validation — no stamping. Real ``Ticket`` / ``Rubric`` rows under the test DB,
mirroring ``tests/teatree_core/test_rubric_model.py``'s convention.
"""

import pytest

from teatree.core.models import Rubric, Ticket
from teatree.core.review.rubric_grading import validate_rubric_grades

# ast-grep-ignore: ac-django-no-pytest-django-db
pytestmark = pytest.mark.django_db


def _ticket() -> Ticket:
    return Ticket.objects.create(overlay="t3-teatree", state=Ticket.State.REVIEW_REQUESTED)


def _rubric(*criteria: str) -> Rubric:
    return Rubric.populate(_ticket(), list(criteria))


class TestNoRubric:
    def test_none_rubric_owes_no_grades(self) -> None:
        error, grades = validate_rubric_grades(None, [{"ordinal": 0, "status": "pass"}], verdict="merge_safe")
        assert error == ""
        assert grades == []


class TestCoverage:
    def test_full_coverage_is_accepted(self) -> None:
        rubric = _rubric("AC1", "AC2")
        payload = [
            {"ordinal": 0, "status": "pass", "rationale": "unit: test_a"},
            {"ordinal": 1, "status": "pass", "rationale": "unit: test_b"},
        ]
        error, grades = validate_rubric_grades(rubric, payload, verdict="merge_safe")
        assert error == ""
        assert {g["ordinal"] for g in grades} == {0, 1}

    def test_ungraded_criterion_is_refused_even_on_a_hold_verdict(self) -> None:
        # Coverage is unconditional: ANY recorded verdict retires the review claim,
        # so a HOLD over an ungraded rubric strands it exactly as merge_safe would.
        rubric = _rubric("AC1", "AC2")
        payload = [{"ordinal": 0, "status": "pass", "rationale": "unit: test_a"}]
        error, grades = validate_rubric_grades(rubric, payload, verdict="hold")
        assert "#1" in error
        assert "ungraded" in error
        assert grades == []

    def test_no_grades_at_all_names_every_criterion_ungraded(self) -> None:
        rubric = _rubric("AC1")
        error, grades = validate_rubric_grades(rubric, None, verdict="merge_safe")
        assert "#0" in error
        assert "you returned no" in error
        assert grades == []

    def test_non_list_payload_names_the_actual_type(self) -> None:
        rubric = _rubric("AC1")
        error, grades = validate_rubric_grades(rubric, {"0": "pass"}, verdict="merge_safe")
        assert "was a dict" in error
        assert grades == []


class TestMergeSafeContradiction:
    def test_merge_safe_over_a_failed_criterion_is_refused(self) -> None:
        rubric = _rubric("AC1", "AC2")
        payload = [
            {"ordinal": 0, "status": "fail"},
            {"ordinal": 1, "status": "pass", "rationale": "unit: test_b"},
        ]
        error, grades = validate_rubric_grades(rubric, payload, verdict="merge_safe")
        assert "#0" in error
        assert "merge_safe" in error
        assert grades == []

    def test_a_hold_verdict_over_a_failed_criterion_is_not_a_contradiction(self) -> None:
        # The contradiction is verdict-scoped: a HOLD legitimately reports a FAIL.
        rubric = _rubric("AC1")
        payload = [{"ordinal": 0, "status": "fail"}]
        error, grades = validate_rubric_grades(rubric, payload, verdict="hold")
        assert error == ""
        assert grades[0]["ordinal"] == 0


class TestMalformedItem:
    def test_a_bare_string_item_is_refused_not_raised(self) -> None:
        rubric = _rubric("AC1")
        error, grades = validate_rubric_grades(rubric, ["pass"], verdict="merge_safe")
        assert "must be an object" in error
        assert grades == []

    def test_a_missing_ordinal_is_refused_not_raised(self) -> None:
        rubric = _rubric("AC1")
        error, grades = validate_rubric_grades(rubric, [{"status": "pass"}], verdict="merge_safe")
        assert "ordinal" in error
        assert grades == []
