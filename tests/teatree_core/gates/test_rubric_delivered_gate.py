"""The rubric done-gate at ``mark_delivered``.

``core.Rubric`` is the single home for the acceptance-criteria list and the plan is its
only producer, so this is the one gate standing between a ticket and DELIVERED on the
acceptance dimension. Nothing here patches a setting — it has no off switch, and that
is itself part of what these tests pin.

One class per condition it enforces:

============================  ===============================================
condition                     block
============================  ===============================================
no rubric at delivered        zero proven criteria is a partial-subset claim
a PENDING/FAIL/uncited PASS   an ungraded or uncited criterion proves nothing
a maker grader                a rubric is graded by an INDEPENDENT verifier
============================  ===============================================

The audited waiver is the human-authorized ``ticket plan-bypass`` — and only that: a
``none_reason`` on an ordinary plan's ``acceptance_criteria`` writes no rows and waives
nothing. A bypass waives a missing or ungraded rubric, never a recorded FAIL. A PASS
rationale is free-form: any test kind (unit, integration, functional, e2e) counts.
"""

import json

import pytest
from django.core.management import call_command
from django.test import TestCase

from teatree.core.gates.rubric_gate import RubricNotVerifiedError, check_rubric_verified
from teatree.core.models import Rubric, Ticket
from teatree.core.models.plan_artifact import PlanArtifact
from teatree.core.models.rubric import PHASE_CRITERIA

_SHA = "a" * 40
_FAILING_CRITERION = "the verifier's FAIL outranks the audited bypass"
_UNGRADED_CRITERION = "the refusal names the criterion it is about"
_END_TO_END_CRITERION = "the CLI producer records a criterion the verifier can grade"


def _retrospected() -> Ticket:
    return Ticket.objects.create(overlay="acme", state=Ticket.State.RETRO_RECORDED)


def _plan(ticket: Ticket, acceptance: dict) -> None:
    PlanArtifact.objects.create(
        ticket=ticket,
        plan_text="real plan",
        recorded_by="planner",
        base_sha=_SHA,
        adequacy={
            "design": {"content": "the approach"},
            "integration_seams": {"none_reason": "leaf module"},
            "edge_cases": {"none_reason": "none"},
            "test_strategy": {"content": "red-first"},
            "acceptance_criteria": acceptance,
        },
    )


def _bypass(ticket: Ticket) -> PlanArtifact:
    return PlanArtifact.record_bypass(
        ticket=ticket, plan_text="[audited bypass by alice] urgent hotfix", recorded_by="alice"
    )


def _graded(ticket: Ticket, *, texts: list[str], rationale: str = "unit: tests/foo.py::test_bar") -> Rubric:
    rubric = Rubric.populate(ticket, texts)
    for criterion in rubric.criteria.all():
        criterion.record_grade(status="pass", grader_identity="cold-reviewer", reviewed_sha=_SHA, rationale=rationale)
    return rubric


class TestNoRubricAtDeliveredBlocks(TestCase):
    """Old: a missing manifest is itself the block. New: a missing rubric is."""

    def test_a_ticket_with_no_rubric_and_no_plan_cannot_deliver(self) -> None:
        ticket = _retrospected()
        with pytest.raises(RubricNotVerifiedError, match="no rubric"):
            check_rubric_verified(ticket)

    def test_a_plan_that_declared_criteria_but_produced_no_rubric_cannot_deliver(self) -> None:
        ticket = _retrospected()
        _plan(ticket, {"content": ["the collapse preserves every old block"]})
        with pytest.raises(RubricNotVerifiedError, match="no rubric"):
            check_rubric_verified(ticket)


class TestAnUngradedCriterionBlocks(TestCase):
    """Old: an uncovered AC blocks. New: PENDING / FAIL / uncited-PASS block."""

    def test_a_pending_criterion_blocks(self) -> None:
        ticket = _retrospected()
        Rubric.populate(ticket, ["the gate refuses an ungraded criterion"])
        with pytest.raises(RubricNotVerifiedError, match="ungraded"):
            check_rubric_verified(ticket)

    def test_a_failed_criterion_blocks(self) -> None:
        ticket = _retrospected()
        rubric = _graded(ticket, texts=["a failed criterion blocks delivery"])
        rubric.criteria.get(ordinal=0).record_grade(status="fail", grader_identity="cold-reviewer", reviewed_sha=_SHA)
        with pytest.raises(RubricNotVerifiedError, match="FAIL"):
            check_rubric_verified(ticket)

    def test_an_uncited_pass_blocks(self) -> None:
        ticket = _retrospected()
        rubric = _graded(ticket, texts=["a PASS must cite the test that proves it"])
        rubric.criteria.filter(ordinal=0).update(rationale="")
        with pytest.raises(RubricNotVerifiedError, match="cite"):
            check_rubric_verified(ticket)

    def test_a_maker_graded_criterion_blocks(self) -> None:
        # record_grade refuses a maker outright, so the row is written directly —
        # cited, so the uncited rung cannot be what fires.
        ticket = _retrospected()
        rubric = Rubric.populate(ticket, ["the maker may not grade their own work"])
        rubric.criteria.filter(ordinal=0).update(
            status="pass", grader_identity="coding-agent", reviewed_sha=_SHA, rationale="unit: tests/foo.py::test_bar"
        )
        with pytest.raises(RubricNotVerifiedError, match="independent verifier"):
            check_rubric_verified(ticket)

    def test_the_refusal_names_the_criterion(self) -> None:
        ticket = _retrospected()
        Rubric.populate(ticket, [_UNGRADED_CRITERION])
        with pytest.raises(RubricNotVerifiedError) as excinfo:
            check_rubric_verified(ticket)
        assert f"#0 {_UNGRADED_CRITERION!r}" in str(excinfo.value)

    def test_a_fully_graded_cited_rubric_passes(self) -> None:
        ticket = _retrospected()
        _graded(ticket, texts=["AC1 is proven", "AC2 is proven"])
        assert check_rubric_verified(ticket) is None


class TestTheDeliveredCheckIsNotHeadBound(TestCase):
    """The SHA bind is the MERGE gate's job; a delivered ticket's head has moved on."""

    def test_a_grade_against_an_older_sha_still_delivers(self) -> None:
        ticket = _retrospected()
        rubric = _graded(ticket, texts=["the delivered check does not re-bind the SHA"])
        rubric.criteria.filter(ordinal=0).update(reviewed_sha="b" * 40)
        assert check_rubric_verified(ticket) is None


class TestThePlanBypassIsTheAuditedWaiver(TestCase):
    """The ONE waiver is the human-authorized ``ticket plan-bypass``, and it never outranks a FAIL."""

    def test_a_none_reason_on_an_ordinary_plan_is_not_a_waiver(self) -> None:
        ticket = _retrospected()
        _plan(ticket, {"none_reason": "docs-only change, no behaviour to accept"})
        with pytest.raises(RubricNotVerifiedError, match="no rubric"):
            check_rubric_verified(ticket)

    def test_the_audited_bypass_is_the_waiver(self) -> None:
        ticket = _retrospected()
        _bypass(ticket)
        assert check_rubric_verified(ticket) is None

    def test_the_waiver_is_read_from_the_latest_plan(self) -> None:
        ticket = _retrospected()
        _bypass(ticket)
        _plan(ticket, {"content": ["a later plan declared real criteria"]})
        with pytest.raises(RubricNotVerifiedError, match="no rubric"):
            check_rubric_verified(ticket)

    def test_a_bypass_waives_the_ungraded_phase_criteria_it_never_declared(self) -> None:
        ticket = _retrospected()
        Rubric.populate(ticket, [PHASE_CRITERIA["coding"]])
        _bypass(ticket)
        assert check_rubric_verified(ticket) is None

    def test_a_bypass_never_delivers_over_a_recorded_fail(self) -> None:
        ticket = _retrospected()
        rubric = _graded(ticket, texts=[_FAILING_CRITERION])
        rubric.criteria.get(ordinal=0).record_grade(status="fail", grader_identity="cold-reviewer", reviewed_sha=_SHA)
        _bypass(ticket)
        with pytest.raises(RubricNotVerifiedError, match="FAIL") as excinfo:
            check_rubric_verified(ticket)
        assert _FAILING_CRITERION in str(excinfo.value)


class TestFsmWiring(TestCase):
    def test_mark_delivered_refuses_an_ungraded_rubric(self) -> None:
        ticket = _retrospected()
        Rubric.populate(ticket, ["the FSM refuses an ungraded rubric"])
        with pytest.raises(RubricNotVerifiedError):
            ticket.mark_delivered()
        ticket.refresh_from_db()
        assert ticket.state == Ticket.State.RETRO_RECORDED

    def test_mark_delivered_passes_a_graded_rubric(self) -> None:
        ticket = _retrospected()
        _graded(ticket, texts=["the FSM delivers a graded rubric"])
        ticket.mark_delivered()
        assert ticket.state == Ticket.State.DELIVERED


class TestTheCliProducerSatisfiesTheGateEndToEnd(TestCase):
    """Plan -> grade -> deliver through the real CLI: the gate is satisfiable, not just refusable."""

    def test_plan_then_grade_then_deliver(self) -> None:
        ticket = _retrospected()
        manifest = {
            "design": {"content": "the approach"},
            "integration_seams": {"none_reason": "leaf module"},
            "edge_cases": {"none_reason": "none"},
            "test_strategy": {"content": "red-first"},
            "acceptance_criteria": {"content": [_END_TO_END_CRITERION]},
        }
        call_command(
            "ticket", "plan", str(ticket.pk), "the plan", "--base-sha", _SHA, "--adequacy-json", json.dumps(manifest)
        )
        call_command(
            "ticket",
            "rubric-grade",
            str(ticket.pk),
            "--grades-json",
            json.dumps([{"ordinal": 0, "status": "pass", "rationale": "unit: this test"}]),
            "--grader-identity",
            "cold-reviewer",
            "--reviewed-sha",
            _SHA,
        )

        ticket.refresh_from_db()
        ticket.mark_delivered()
        assert ticket.state == Ticket.State.DELIVERED
