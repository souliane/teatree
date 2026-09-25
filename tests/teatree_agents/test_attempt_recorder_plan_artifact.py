"""The planning envelope is the producer: agent result -> PlanArtifact + Rubric rows.

Two things are pinned here. The phase-alias regression (integration audit #20): a task
stored with the accepted short verb ``"plan"`` recorded no ``PlanArtifact``, so the plan
gate refused ``STARTED -> PLANNED`` and the ticket wedged at ``STARTED``.

And the functional contract this file exists for: a REAL planning envelope, carrying the
five-section manifest a planner emits, driven through ``record_result_envelope`` produces
BOTH the plan row and the ticket's rubric — the acceptance criteria have one home, and the
plan is what fills it. Nothing here patches a setting; the enforcement has no off switch.
"""

import pytest
from django.test import TestCase

from teatree.agents.attempt_recorder import record_result_envelope, validate_result_keys
from teatree.core.models import Rubric, RubricCriterion, Session, Task, Ticket
from teatree.core.models.errors import NoPlanArtifactError
from teatree.core.models.plan_adequacy import all_negated_adequacy
from teatree.core.models.plan_artifact import PlanArtifact
from teatree.core.models.rubric import PHASE_CRITERIA

_FORTY_HEX = "a" * 40
_CITATION = "unit: tests/teatree_agents/test_attempt_recorder_plan_artifact.py"

_CRITERIA = [
    "a planning envelope produces the ticket's rubric rows",
    "a thin envelope is refused before any row is written",
]


def _full_adequacy() -> dict:
    return {
        "design": {"content": "carry base_sha + adequacy through the result envelope"},
        "integration_seams": {"content": ["src/teatree/agents/result_schema.py"]},
        "edge_cases": {"none_reason": "no edges: the recorder already reads both keys"},
        "test_strategy": {"content": "record a planning envelope and assert both rows"},
        "acceptance_criteria": {"content": list(_CRITERIA)},
    }


def _envelope(**overrides: object) -> dict:
    return {"plan_text": "the plan", "base_sha": _FORTY_HEX, "adequacy": _full_adequacy(), **overrides}


def _planning_task(*, phase: str = "planning") -> Task:
    ticket = Ticket.objects.create(role=Ticket.Role.AUTHOR, state=Ticket.State.STARTED, overlay="acme")
    session = Session.objects.create(ticket=ticket, agent_id=phase)
    task = Task.objects.create(ticket=ticket, session=session, phase=phase)
    task.claim(claimed_by="loop-slot")
    return task


class TestPlanArtifactPhaseAlias(TestCase):
    def test_short_verb_plan_records_artifact_and_advances(self) -> None:
        task = _planning_task(phase="plan")
        record_result_envelope(task, _envelope(), phase="plan")
        task.refresh_from_db()
        task.ticket.refresh_from_db()
        assert PlanArtifact.objects.filter(ticket=task.ticket).exists()
        assert task.ticket.state == Ticket.State.PLANNED
        assert task.status == Task.Status.COMPLETED

    def test_canonical_planning_records_artifact_and_advances(self) -> None:
        task = _planning_task(phase="planning")
        record_result_envelope(task, _envelope(), phase="planning")
        task.ticket.refresh_from_db()
        assert PlanArtifact.objects.filter(ticket=task.ticket).exists()
        assert task.ticket.state == Ticket.State.PLANNED

    def test_ticket_is_not_stranded_at_started(self) -> None:
        task = _planning_task(phase="plan")
        record_result_envelope(task, _envelope(), phase="plan")
        task.ticket.refresh_from_db()
        assert task.ticket.state != Ticket.State.STARTED

    def test_phase_from_task_field_when_envelope_phase_blank(self) -> None:
        task = _planning_task(phase="plan")
        record_result_envelope(task, _envelope())
        assert PlanArtifact.objects.filter(ticket=task.ticket).exists()

    def test_non_planning_phase_records_no_artifact(self) -> None:
        ticket = Ticket.objects.create(role=Ticket.Role.AUTHOR, state=Ticket.State.STARTED)
        session = Session.objects.create(ticket=ticket, agent_id="code")
        task = Task.objects.create(ticket=ticket, session=session, phase="code")
        task.claim(claimed_by="loop-slot")
        record_result_envelope(
            task,
            {"plan_text": "ignored", "files_modified": [{"path": "a.py", "action": "modified"}]},
            phase="code",
        )
        assert not PlanArtifact.objects.filter(ticket=ticket).exists()

    def test_empty_plan_text_records_no_artifact(self) -> None:
        task = _planning_task(phase="plan")
        with pytest.raises(NoPlanArtifactError):
            record_result_envelope(task, _envelope(plan_text="   "), phase="plan")
        assert not PlanArtifact.objects.filter(ticket=task.ticket).exists()


class TestAPlanningEnvelopeProducesTheRubric(TestCase):
    """The functional proof of the collapse: one envelope, both rows, no setting."""

    def test_a_real_planning_envelope_records_the_plan_and_its_rubric(self) -> None:
        task = _planning_task()
        record_result_envelope(task, _envelope(), phase="planning")
        task.refresh_from_db()
        task.ticket.refresh_from_db()

        artifact = PlanArtifact.objects.get(ticket=task.ticket)
        assert artifact.base_sha == _FORTY_HEX
        assert artifact.adequacy == _full_adequacy()

        rubric = Rubric.objects.active_for_ticket(task.ticket)
        assert rubric is not None
        assert set(_CRITERIA) <= {c.text for c in rubric.criteria.all()}

        assert task.ticket.state == Ticket.State.PLANNED
        assert task.status == Task.Status.COMPLETED

    def test_the_plans_criteria_are_added_beside_the_seeded_phase_criterion(self) -> None:
        # The phase criterion is seeded AND graded before the plan lands, so a destructive
        # populate cannot be masked by `Task.complete` re-seeding the same text afterwards.
        task = _planning_task()
        task.session.visit_phase("planning")
        seeded = Rubric.objects.active_for_ticket(task.ticket)
        assert seeded is not None
        seeded.criteria.get(ordinal=0).record_grade(
            status="pass", grader_identity="cold-reviewer", reviewed_sha=_FORTY_HEX, rationale=_CITATION
        )

        record_result_envelope(task, _envelope(), phase="planning")

        rubric = Rubric.objects.active_for_ticket(task.ticket)
        assert rubric is not None
        assert {c.text for c in rubric.criteria.all()} == {PHASE_CRITERIA["planning"], *_CRITERIA}
        survived = rubric.criteria.get(text=PHASE_CRITERIA["planning"])
        assert survived.status == RubricCriterion.Status.PASS
        assert survived.grader_identity == "cold-reviewer"

    def test_a_bound_adequate_envelope_passes_key_validation(self) -> None:
        assert validate_result_keys(_envelope()) == ""

    def test_a_thin_envelope_fails_the_attempt_rather_than_the_recorder(self) -> None:
        # A planner refusal is the PLANNER's failure, so it lands as a FAILED attempt the
        # requeue sweep can see — not an exception out of the recorder, which strands the
        # task CLAIMED with no attempt and no diagnostic.
        task = _planning_task()
        attempt = record_result_envelope(task, {"plan_text": "scope: X\nacceptance: Y"}, phase="planning")
        task.refresh_from_db()
        assert "base_sha" in attempt.error
        assert task.status == Task.Status.FAILED
        assert not PlanArtifact.objects.filter(ticket=task.ticket).exists()
        assert Rubric.objects.active_for_ticket(task.ticket) is None

    def test_an_unfalsifiable_criterion_fails_the_attempt_not_the_recorder(self) -> None:
        adequacy = _full_adequacy()
        adequacy["acceptance_criteria"] = {"content": ["the existing resolver test suite passes UNMODIFIED"]}
        task = _planning_task()
        attempt = record_result_envelope(task, _envelope(adequacy=adequacy), phase="planning")
        task.refresh_from_db()
        assert "INACTION" in attempt.error
        assert task.status == Task.Status.FAILED
        assert not PlanArtifact.objects.filter(ticket=task.ticket).exists()

    def test_the_planner_envelope_cannot_waive_the_rubric(self) -> None:
        # The all-negatives manifest IS the human-authorized plan-bypass, so a planner
        # emitting it would waive the rubric gate with nobody having authorized anything.
        task = _planning_task()
        envelope = _envelope(adequacy=dict(all_negated_adequacy("nothing to declare")))
        attempt = record_result_envelope(task, envelope, phase="planning")
        task.refresh_from_db()
        assert "plan-bypass" in attempt.error
        assert task.status == Task.Status.FAILED
        assert not PlanArtifact.objects.filter(ticket=task.ticket).exists()

    def test_an_envelope_declaring_no_criteria_adds_none_of_its_own(self) -> None:
        task = _planning_task()
        adequacy = _full_adequacy()
        adequacy["acceptance_criteria"] = {"none_reason": "docs-only ticket, no behaviour to accept"}
        record_result_envelope(task, _envelope(adequacy=adequacy), phase="planning")
        assert PlanArtifact.objects.filter(ticket=task.ticket).exists()
        rubric = Rubric.objects.active_for_ticket(task.ticket)
        assert rubric is not None
        assert {c.text for c in rubric.criteria.all()} == {PHASE_CRITERIA["planning"]}
