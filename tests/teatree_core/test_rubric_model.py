"""The per-ticket rubric model — population, the grade factory, the truth table (#2241).

Population, the guarded grade factory, and the fail-closed ``is_fully_passed_at``
truth table. Real ``Ticket`` / ``Rubric`` rows under the test DB; only pure-logic predicates are
exercised here (the merge-precondition wiring lives in the integration test). The
guarded :meth:`RubricCriterion.record_grade` factory shares ``MergeClear``'s
validation primitives, so the maker / bad-SHA refusals mirror the CLEAR contract.
"""

import pytest
from django.test import TestCase

from teatree.core.models import Rubric, RubricCriterion, RubricError, Session, Ticket
from teatree.core.models.rubric import PHASE_CRITERIA
from teatree.quality.falsifiable_criteria import falsifiability_violation

# ast-grep-ignore: ac-django-no-pytest-django-db
pytestmark = pytest.mark.django_db

_SHA = "a" * 40
_OTHER_SHA = "b" * 40
_CITATION = "unit: tests/teatree_core/test_rubric_model.py::TestRecordGrade"


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

    def test_populate_refuses_a_rubric_whose_criteria_are_all_satisfiable_by_inaction(self) -> None:
        # "the existing resolver test suite passes UNMODIFIED" is satisfied by
        # never touching the resolver, so it certifies a skipped phase as a success.
        ticket = _ticket()
        with pytest.raises(RubricError, match="satisfiable by INACTION"):
            Rubric.populate(ticket, ["the existing resolver test suite passes UNMODIFIED"])
        assert ticket.rubrics.count() == 0

    def test_populate_accepts_an_absence_criterion_paired_with_a_positive_one(self) -> None:
        rubric = Rubric.populate(
            _ticket(),
            [
                "the resolver reads defaults.toml, so editing a value there changes the resolved setting",
                "the existing resolver test suite passes UNMODIFIED",
            ],
        )
        assert rubric.criteria.count() == 2


class TestRecordGrade(TestCase):
    def _criterion(self) -> RubricCriterion:
        rubric = Rubric.populate(_ticket(), ["AC1"])
        return rubric.criteria.get(ordinal=0)

    def test_record_grade_stamps_pass(self) -> None:
        criterion = self._criterion()
        criterion.record_grade(status="pass", grader_identity="cold-reviewer", reviewed_sha=_SHA, rationale=_CITATION)
        criterion.refresh_from_db()
        assert criterion.status == RubricCriterion.Status.PASS
        assert criterion.grader_identity == "cold-reviewer"
        assert criterion.reviewed_sha == _SHA
        assert criterion.rationale == _CITATION
        assert criterion.graded_at is not None

    def test_record_grade_rejects_pending_status(self) -> None:
        with pytest.raises(RubricError, match="not a grade"):
            self._criterion().record_grade(status="pending", grader_identity="cold-reviewer", reviewed_sha=_SHA)

    def test_record_grade_rejects_maker_grader(self) -> None:
        with pytest.raises(RubricError, match="maker/coding-agent/loop"):
            self._criterion().record_grade(
                status="pass", grader_identity="merge-loop", reviewed_sha=_SHA, rationale=_CITATION
            )

    def test_record_grade_rejects_empty_grader(self) -> None:
        with pytest.raises(RubricError, match="required"):
            self._criterion().record_grade(status="pass", grader_identity="  ", reviewed_sha=_SHA, rationale=_CITATION)

    def test_record_grade_rejects_truncated_sha(self) -> None:
        with pytest.raises(RubricError, match="40-char hex"):
            self._criterion().record_grade(
                status="pass", grader_identity="cold-reviewer", reviewed_sha="abc123", rationale=_CITATION
            )

    def test_a_pass_without_a_rationale_is_refused(self) -> None:
        with pytest.raises(RubricError, match="rationale"):
            self._criterion().record_grade(status="pass", grader_identity="cold-reviewer", reviewed_sha=_SHA)

    def test_a_pass_rationale_may_cite_any_kind_of_test(self) -> None:
        for rationale in (
            "unit: tests/teatree_core/test_rubric_model.py::test_a_pass_without_a_rationale_is_refused",
            "integration: the delivered-gate suite exercises this end to end",
            "functional: drove a planning envelope through attempt_recorder",
            "e2e: playwright/dash/settings.spec.ts covers the edit path",
        ):
            with self.subTest(rationale=rationale):
                criterion = self._criterion()
                criterion.record_grade(
                    status="pass", grader_identity="cold-reviewer", reviewed_sha=_SHA, rationale=rationale
                )
                criterion.refresh_from_db()
                assert criterion.rationale == rationale

    def test_a_fail_needs_no_rationale(self) -> None:
        criterion = self._criterion()
        criterion.record_grade(status="fail", grader_identity="cold-reviewer", reviewed_sha=_SHA)
        criterion.refresh_from_db()
        assert criterion.status == RubricCriterion.Status.FAIL


class TestIsFullyPassedAt(TestCase):
    def _graded_rubric(self, *, sha: str = _SHA, grader: str = "cold-reviewer") -> Rubric:
        rubric = Rubric.populate(_ticket(), ["AC1", "AC2"])
        for criterion in rubric.criteria.all():
            criterion.record_grade(status="pass", grader_identity=grader, reviewed_sha=sha, rationale=_CITATION)
        return rubric

    def test_all_pass_at_head_is_fully_passed(self) -> None:
        rubric = self._graded_rubric()
        assert rubric.is_fully_passed_at(_SHA) is True

    def test_empty_rubric_is_not_passed(self) -> None:
        rubric = Rubric.objects.create(ticket=_ticket())
        assert rubric.is_fully_passed_at(_SHA) is False
        assert "no criteria" in rubric.unverified_reason(_SHA)

    def test_one_pending_criterion_is_not_passed(self) -> None:
        rubric = Rubric.populate(_ticket(), ["AC1", "AC2"])
        rubric.criteria.get(ordinal=0).record_grade(
            status="pass", grader_identity="cold-reviewer", reviewed_sha=_SHA, rationale=_CITATION
        )
        assert rubric.is_fully_passed_at(_SHA) is False
        assert "ungraded" in rubric.unverified_reason(_SHA)
        assert "#1 'AC2'" in rubric.unverified_reason(_SHA)

    def test_one_failed_criterion_is_not_passed(self) -> None:
        rubric = self._graded_rubric()
        failing = rubric.criteria.get(ordinal=0)
        failing.record_grade(status="fail", grader_identity="cold-reviewer", reviewed_sha=_SHA)  # a FAIL needs none
        assert rubric.is_fully_passed_at(_SHA) is False
        assert "FAIL" in rubric.unverified_reason(_SHA)
        assert "#0 'AC1'" in rubric.unverified_reason(_SHA)

    def test_stale_sha_grade_is_not_passed(self) -> None:
        rubric = self._graded_rubric(sha=_OTHER_SHA)
        assert rubric.is_fully_passed_at(_SHA) is False
        assert "stale" in rubric.unverified_reason(_SHA)
        assert "#0 'AC1'" in rubric.unverified_reason(_SHA)

    def test_empty_head_sha_is_not_passed(self) -> None:
        assert self._graded_rubric().is_fully_passed_at("") is False

    def test_unverified_reason_is_empty_when_fully_passing(self) -> None:
        assert self._graded_rubric().unverified_reason(_SHA) == ""

    def test_the_delivered_ladder_drops_only_the_head_bind(self) -> None:
        # No head_sha: the stale rung is the merge gate's, and a delivered ticket's
        # head has long since moved off the reviewed one.
        rubric = self._graded_rubric(sha=_OTHER_SHA)
        assert rubric.unverified_reason() == ""
        assert "stale" in rubric.unverified_reason(_SHA)

    def test_a_waived_rubric_keeps_only_the_fail_rung(self) -> None:
        rubric = Rubric.populate(_ticket(), ["AC1", "AC2"])
        assert rubric.unverified_reason(_SHA, waived=True) == ""
        rubric.criteria.get(ordinal=0).record_grade(status="fail", grader_identity="cold-reviewer", reviewed_sha=_SHA)
        assert "FAIL" in rubric.unverified_reason(_SHA, waived=True)

    def test_a_waived_empty_rubric_is_verified(self) -> None:
        rubric = Rubric.objects.create(ticket=_ticket())
        assert rubric.unverified_reason(_SHA, waived=True) == ""

    def test_an_uncited_pass_is_not_passed(self) -> None:
        # A legacy row graded before a citation was required: record_grade refuses this
        # shape now, so it is written through the ORM to model what is already on disk.
        rubric = self._graded_rubric()
        rubric.criteria.filter(ordinal=0).update(rationale="")
        assert rubric.is_fully_passed_at(_SHA) is False
        assert "cite" in rubric.unverified_reason(_SHA)
        assert "#0 'AC1'" in rubric.unverified_reason(_SHA)

    def test_str_renders_ticket_and_criteria_counts(self) -> None:
        rubric = Rubric.populate(_ticket(), ["AC1", "AC2"])
        assert str(rubric) == f"rubric<ticket={rubric.ticket_id} criteria=2>"
        criterion = rubric.criteria.get(ordinal=0)
        assert str(criterion) == f"criterion<rubric={rubric.pk} #0 pending>"


class TestThePhaseRubricHasContentOnDayOne(TestCase):
    """`core.Rubric` shipped with a CLI, a manager, a gate and an error type — and zero rows.

    A checklist nobody writes is a checklist nobody grades, so the gate over it could only
    ever be armed against an empty table. The probe-first standard is exactly the content it
    lacked: the factory has to hold itself to it without the owner in the loop, and a
    criterion is how that becomes gradeable rather than a sentence in a skill.
    """

    def test_entering_a_phase_seeds_that_phases_criterion(self) -> None:
        ticket = _ticket()
        session = Session.objects.create(ticket=ticket)
        session.visit_phase("planning")
        rubric = Rubric.objects.active_for_ticket(ticket)
        assert rubric is not None
        assert [c.text for c in rubric.criteria_set.all()] == [PHASE_CRITERIA["planning"]]

    def test_a_second_phase_adds_rather_than_replaces(self) -> None:
        # Replacing would reset every grade, so a ticket graded on its plan would arrive at
        # coding with that grade silently gone.
        ticket = _ticket()
        session = Session.objects.create(ticket=ticket)
        session.visit_phase("planning")
        session.visit_phase("coding")
        texts = {c.text for c in Rubric.objects.active_for_ticket(ticket).criteria_set.all()}
        assert texts == {PHASE_CRITERIA["planning"], PHASE_CRITERIA["coding"]}

    def test_re_entering_a_phase_adds_nothing(self) -> None:
        ticket = _ticket()
        session = Session.objects.create(ticket=ticket)
        session.visit_phase("planning")
        session.visit_phase("planning")
        assert Rubric.objects.active_for_ticket(ticket).criteria_set.count() == 1

    def test_an_operators_own_criteria_survive_the_seed(self) -> None:
        ticket = _ticket()
        Rubric.populate(ticket, ["the endpoint returns 201 for a fresh idempotency key"])
        session = Session.objects.create(ticket=ticket)
        session.visit_phase("planning")
        texts = {c.text for c in Rubric.objects.active_for_ticket(ticket).criteria_set.all()}
        assert "the endpoint returns 201 for a fresh idempotency key" in texts
        assert PHASE_CRITERIA["planning"] in texts

    def test_a_phase_with_no_template_seeds_nothing(self) -> None:
        # The control: the seed is keyed on the TEMPLATE, so a phase without one is untouched
        # rather than given an empty or generic criterion.
        ticket = _ticket()
        Session.objects.create(ticket=ticket).visit_phase("shipping")
        assert Rubric.objects.active_for_ticket(ticket) is None

    def test_every_template_criterion_is_falsifiable(self) -> None:
        # A criterion satisfied by inaction certifies nothing — the population guard would
        # refuse a checklist of them, and a seeded one must never be that shape.
        assert falsifiability_violation(list(PHASE_CRITERIA.values())) == ""
        for text in PHASE_CRITERIA.values():
            assert falsifiability_violation([text]) == "", text

    def test_a_seeded_criterion_is_gradeable_through_the_existing_factory(self) -> None:
        ticket = _ticket()
        Session.objects.create(ticket=ticket).visit_phase("planning")
        criterion = Rubric.objects.active_for_ticket(ticket).criteria_set.get()
        criterion.record_grade(status=RubricCriterion.Status.FAIL, grader_identity="reviewer", reviewed_sha="a" * 40)
        criterion.refresh_from_db()
        assert criterion.status == RubricCriterion.Status.FAIL


class TestApplyGrades(TestCase):
    """The batch seam both the CLI and the reviewing recorder stamp grades through."""

    def test_a_full_batch_grades_every_named_criterion(self) -> None:
        rubric = Rubric.populate(_ticket(), ["AC1", "AC2"])

        graded = rubric.apply_grades(
            [
                {"ordinal": 0, "status": "pass", "rationale": _CITATION},
                {"ordinal": 1, "status": "fail"},
            ],
            grader_identity="cold-reviewer-agent",
            reviewed_sha=_SHA,
        )

        assert graded == 2
        statuses = [c.status for c in rubric.criteria.all()]
        assert statuses == [RubricCriterion.Status.PASS, RubricCriterion.Status.FAIL]

    def test_an_unknown_ordinal_is_refused_by_name(self) -> None:
        rubric = Rubric.populate(_ticket(), ["AC1"])

        with pytest.raises(RubricError, match="ordinal 7"):
            rubric.apply_grades(
                [{"ordinal": 7, "status": "pass", "rationale": _CITATION}],
                grader_identity="cold-reviewer-agent",
                reviewed_sha=_SHA,
            )

    def test_a_refusal_mid_batch_stamps_nothing(self) -> None:
        # The atomicity pin: criterion 0 is valid and criterion 1's PASS is uncited,
        # so a non-atomic run leaves 0 stamped and the rubric half-graded.
        rubric = Rubric.populate(_ticket(), ["AC1", "AC2"])

        with pytest.raises(RubricError, match="rationale"):
            rubric.apply_grades(
                [
                    {"ordinal": 0, "status": "pass", "rationale": _CITATION},
                    {"ordinal": 1, "status": "pass"},
                ],
                grader_identity="cold-reviewer-agent",
                reviewed_sha=_SHA,
            )

        assert [c.status for c in rubric.criteria.all()] == [
            RubricCriterion.Status.PENDING,
            RubricCriterion.Status.PENDING,
        ]

    def test_an_unknown_ordinal_after_a_valid_one_stamps_nothing(self) -> None:
        rubric = Rubric.populate(_ticket(), ["AC1"])

        with pytest.raises(RubricError):
            rubric.apply_grades(
                [
                    {"ordinal": 0, "status": "pass", "rationale": _CITATION},
                    {"ordinal": 9, "status": "pass", "rationale": _CITATION},
                ],
                grader_identity="cold-reviewer-agent",
                reviewed_sha=_SHA,
            )

        assert rubric.criteria.get(ordinal=0).status == RubricCriterion.Status.PENDING


class TestUngradedOrdinals(TestCase):
    """Which criteria a returned grade list leaves PENDING — the coverage question."""

    def test_a_complete_batch_leaves_none_ungraded(self) -> None:
        rubric = Rubric.populate(_ticket(), ["AC1", "AC2"])

        assert rubric.ungraded_ordinals([{"ordinal": 0, "status": "pass"}, {"ordinal": 1, "status": "fail"}]) == []

    def test_a_partial_batch_names_the_ordinals_it_omits(self) -> None:
        rubric = Rubric.populate(_ticket(), ["AC1", "AC2", "AC3"])

        assert rubric.ungraded_ordinals([{"ordinal": 1, "status": "pass"}]) == [0, 2]

    def test_an_empty_batch_leaves_every_ordinal_ungraded(self) -> None:
        rubric = Rubric.populate(_ticket(), ["AC1", "AC2"])

        assert rubric.ungraded_ordinals([]) == [0, 1]

    def test_an_extra_ordinal_no_criterion_carries_is_not_coverage(self) -> None:
        rubric = Rubric.populate(_ticket(), ["AC1"])

        assert rubric.ungraded_ordinals([{"ordinal": 4, "status": "pass"}]) == [0]
