"""The per-ticket rubric model — population, the grade factory, the truth table (#2241).

Population, the guarded grade factory, and the fail-closed ``is_fully_passed_at``
truth table. Real ``Ticket`` / ``Rubric`` rows under the test DB; only pure-logic predicates are
exercised here (the merge-precondition wiring lives in the integration test). The
guarded :meth:`RubricCriterion.record_grade` factory shares ``MergeClear``'s
validation primitives, so the maker / bad-SHA refusals mirror the CLEAR contract.
"""

import pytest
from django.test import TestCase

from teatree.core.models import Rubric, RubricCriterion, RubricError, Ticket

# ast-grep-ignore: ac-django-no-pytest-django-db
pytestmark = pytest.mark.django_db

_SHA = "a" * 40
_OTHER_SHA = "b" * 40


def _ticket() -> Ticket:
    return Ticket.objects.create(overlay="t3-teatree", state=Ticket.State.IN_REVIEW)


class TestRubricPopulation(TestCase):
    def test_populate_creates_pending_criteria(self) -> None:
        rubric = Rubric.populate(_ticket(), ["AC1", "AC2", "AC3"])
        criteria = list(rubric.criteria.all())
        assert [c.text for c in criteria] == ["AC1", "AC2", "AC3"]
        assert [c.ordinal for c in criteria] == [0, 1, 2]
        assert all(c.status == RubricCriterion.Status.PENDING for c in criteria)

    def test_populate_is_get_or_create_and_replaces_criteria(self) -> None:
        ticket = _ticket()
        first = Rubric.populate(ticket, ["old1", "old2"])
        second = Rubric.populate(ticket, ["new1"])
        assert first.pk == second.pk
        assert ticket.rubrics.count() == 1
        assert [c.text for c in second.criteria.all()] == ["new1"]

    def test_populate_strips_blanks_and_refuses_an_empty_rubric(self) -> None:
        ticket = _ticket()
        with pytest.raises(RubricError, match="at least one non-empty criterion"):
            Rubric.populate(ticket, ["   ", ""])
        assert ticket.rubrics.count() == 0


class TestRecordGrade(TestCase):
    def _criterion(self) -> RubricCriterion:
        rubric = Rubric.populate(_ticket(), ["AC1"])
        return rubric.criteria.get(ordinal=0)

    def test_record_grade_stamps_pass(self) -> None:
        criterion = self._criterion()
        criterion.record_grade(status="pass", grader_identity="cold-reviewer", reviewed_sha=_SHA)
        criterion.refresh_from_db()
        assert criterion.status == RubricCriterion.Status.PASS
        assert criterion.grader_identity == "cold-reviewer"
        assert criterion.reviewed_sha == _SHA
        assert criterion.graded_at is not None

    def test_record_grade_rejects_pending_status(self) -> None:
        with pytest.raises(RubricError, match="not a grade"):
            self._criterion().record_grade(status="pending", grader_identity="cold-reviewer", reviewed_sha=_SHA)

    def test_record_grade_rejects_maker_grader(self) -> None:
        with pytest.raises(RubricError, match="maker/coding-agent/loop"):
            self._criterion().record_grade(status="pass", grader_identity="merge-loop", reviewed_sha=_SHA)

    def test_record_grade_rejects_empty_grader(self) -> None:
        with pytest.raises(RubricError, match="required"):
            self._criterion().record_grade(status="pass", grader_identity="  ", reviewed_sha=_SHA)

    def test_record_grade_rejects_truncated_sha(self) -> None:
        with pytest.raises(RubricError, match="40-char hex"):
            self._criterion().record_grade(status="pass", grader_identity="cold-reviewer", reviewed_sha="abc123")


class TestIsFullyPassedAt(TestCase):
    def _graded_rubric(self, *, sha: str = _SHA, grader: str = "cold-reviewer") -> Rubric:
        rubric = Rubric.populate(_ticket(), ["AC1", "AC2"])
        for criterion in rubric.criteria.all():
            criterion.record_grade(status="pass", grader_identity=grader, reviewed_sha=sha)
        return rubric

    def test_all_pass_at_head_is_fully_passed(self) -> None:
        rubric = self._graded_rubric()
        assert rubric.is_fully_passed_at(_SHA) is True

    def test_empty_rubric_is_not_passed(self) -> None:
        rubric = Rubric.objects.create(ticket=_ticket())
        assert rubric.is_fully_passed_at(_SHA) is False
        assert "no criteria" in rubric.block_reason(_SHA)

    def test_one_pending_criterion_is_not_passed(self) -> None:
        rubric = Rubric.populate(_ticket(), ["AC1", "AC2"])
        rubric.criteria.get(ordinal=0).record_grade(status="pass", grader_identity="cold-reviewer", reviewed_sha=_SHA)
        assert rubric.is_fully_passed_at(_SHA) is False
        assert "ungraded" in rubric.block_reason(_SHA)

    def test_one_failed_criterion_is_not_passed(self) -> None:
        rubric = self._graded_rubric()
        failing = rubric.criteria.get(ordinal=0)
        failing.record_grade(status="fail", grader_identity="cold-reviewer", reviewed_sha=_SHA)
        assert rubric.is_fully_passed_at(_SHA) is False
        assert "FAIL" in rubric.block_reason(_SHA)

    def test_stale_sha_grade_is_not_passed(self) -> None:
        rubric = self._graded_rubric(sha=_OTHER_SHA)
        assert rubric.is_fully_passed_at(_SHA) is False
        assert "stale" in rubric.block_reason(_SHA)

    def test_empty_head_sha_is_not_passed(self) -> None:
        assert self._graded_rubric().is_fully_passed_at("") is False

    def test_block_reason_is_empty_when_fully_passing(self) -> None:
        assert self._graded_rubric().block_reason(_SHA) == ""

    def test_str_renders_ticket_and_criteria_counts(self) -> None:
        rubric = Rubric.populate(_ticket(), ["AC1", "AC2"])
        assert str(rubric) == f"rubric<ticket={rubric.ticket_id} criteria=2>"
        criterion = rubric.criteria.get(ordinal=0)
        assert str(criterion) == f"criterion<rubric={rubric.pk} #0 pending>"
