"""PlanArtifact.record() adequacy enforcement — unconditional, and the rubric it produces.

A NEW row needs a 40-char base_sha AND a complete five-section manifest; a
scope+acceptance-only thin spec is REFUSED before any row is written. There is no
setting to turn this off — the tests below patch nothing, which is the control that
the flag is genuinely gone.

The fifth section is ``acceptance_criteria``, and it is a PRODUCER: a plan that
declares criteria populates the ticket's ``core.Rubric`` in the same atomic, so the
plan is the single home for the AC list. A reasoned-negative section writes no rubric —
and waives nothing: the all-negatives manifest is the human-authorized plan-bypass, so
``record`` refuses that shape and points at ``ticket plan-bypass`` instead.
"""

import pytest
from django.test import TestCase

from teatree.core.models import Rubric, RubricCriterion, Session, Ticket
from teatree.core.models.plan_adequacy import all_negated_adequacy
from teatree.core.models.plan_artifact import PlanArtifact
from teatree.core.models.rubric import PHASE_CRITERIA

_FORTY_HEX = "a" * 40
_OTHER_HEX = "b" * 40
_CITATION = "unit: tests/teatree_core/models/test_plan_artifact_record.py"


def _full_adequacy() -> dict:
    return {
        "design": {"content": "record base_sha + adequacy on PlanArtifact"},
        "integration_seams": {"content": ["src/teatree/core/gates/plan_currency_gate.py"]},
        "edge_cases": {"content": ["legacy blank-sha rows"]},
        "test_strategy": {"content": "red-first thin-spec refusal"},
        "acceptance_criteria": {"content": ["a thin plan is refused with no setting patched"]},
    }


def _ticket() -> Ticket:
    return Ticket.objects.create(overlay="acme", state=Ticket.State.WORK_STARTED)


class TestEnforcementIsUnconditional(TestCase):
    """No setting is patched anywhere in this class — the enforcement has no off switch."""

    def test_thin_spec_is_refused_on_the_adequacy_dimension(self) -> None:
        ticket = _ticket()
        with pytest.raises(ValueError, match="five-section adequacy manifest"):
            PlanArtifact.record(
                ticket=ticket, plan_text="scope: X\nacceptance: Y", recorded_by="op", base_sha=_FORTY_HEX
            )
        assert PlanArtifact.objects.filter(ticket=ticket).count() == 0  # refuse-before-write

    def test_fully_thin_plan_is_refused(self) -> None:
        ticket = _ticket()
        with pytest.raises(ValueError, match="base_sha"):
            PlanArtifact.record(ticket=ticket, plan_text="scope: X\nacceptance: Y", recorded_by="op")
        assert PlanArtifact.objects.filter(ticket=ticket).count() == 0

    def test_missing_base_sha_is_refused(self) -> None:
        with pytest.raises(ValueError, match="base_sha"):
            PlanArtifact.record(ticket=_ticket(), plan_text="real plan", recorded_by="op", adequacy=_full_adequacy())

    def test_short_base_sha_is_refused(self) -> None:
        with pytest.raises(ValueError, match="base_sha"):
            PlanArtifact.record(
                ticket=_ticket(), plan_text="real plan", recorded_by="op", base_sha="abc123", adequacy=_full_adequacy()
            )

    def test_a_single_silent_section_is_refused(self) -> None:
        manifest = _full_adequacy()
        manifest["test_strategy"] = {}
        with pytest.raises(ValueError, match="five-section"):
            PlanArtifact.record(
                ticket=_ticket(), plan_text="real plan", recorded_by="op", base_sha=_FORTY_HEX, adequacy=manifest
            )

    def test_a_missing_acceptance_criteria_section_is_refused(self) -> None:
        manifest = _full_adequacy()
        del manifest["acceptance_criteria"]
        with pytest.raises(ValueError, match="five-section"):
            PlanArtifact.record(
                ticket=_ticket(), plan_text="real plan", recorded_by="op", base_sha=_FORTY_HEX, adequacy=manifest
            )

    def test_a_plan_declaring_nothing_is_refused_and_pointed_at_plan_bypass(self) -> None:
        # The all-negatives shape waives the rubric gate, so only the audited
        # `record_bypass` may write it — `record` names the escape instead.
        ticket = _ticket()
        with pytest.raises(ValueError, match="plan-bypass"):
            PlanArtifact.record(
                ticket=ticket,
                plan_text="nothing to declare",
                recorded_by="op",
                base_sha=_FORTY_HEX,
                adequacy=dict(all_negated_adequacy("nothing to declare")),
            )
        assert PlanArtifact.objects.filter(ticket=ticket).count() == 0

    def test_a_substantive_manifest_annotated_with_none_reasons_is_still_recorded(self) -> None:
        """The trap: every section speaks AND carries a reason, so the waiver predicate fired.

        `record` then refused it for declaring NOTHING — a false claim about a plan that
        declares everything — and pointed the planner at `plan-bypass`.
        """
        manifest = _full_adequacy()
        for name in ("integration_seams", "edge_cases", "test_strategy", "acceptance_criteria"):
            manifest[name]["none_reason"] = "annotated by the planner"

        artifact = PlanArtifact.record(
            ticket=_ticket(), plan_text="real plan", recorded_by="op", base_sha=_FORTY_HEX, adequacy=manifest
        )

        assert artifact.pk is not None
        assert [c.text for c in artifact.ticket.rubrics.get().criteria.all()] == [
            "a thin plan is refused with no setting patched"
        ]

    def test_adequate_bound_plan_is_recorded(self) -> None:
        artifact = PlanArtifact.record(
            ticket=_ticket(), plan_text="real plan", recorded_by="op", base_sha=_FORTY_HEX, adequacy=_full_adequacy()
        )
        assert artifact.base_sha == _FORTY_HEX
        assert artifact.adequacy["integration_seams"]["content"] == ["src/teatree/core/gates/plan_currency_gate.py"]


class TestRecordPopulatesTheRubric(TestCase):
    def test_declared_criteria_become_rubric_rows(self) -> None:
        ticket = _ticket()
        manifest = _full_adequacy()
        manifest["acceptance_criteria"] = {
            "content": [
                "the plan is the single home for the AC list",
                "a PASS grade cites the test that proves it",
            ]
        }
        PlanArtifact.record(
            ticket=ticket, plan_text="real plan", recorded_by="op", base_sha=_FORTY_HEX, adequacy=manifest
        )
        rubric = Rubric.objects.active_for_ticket(ticket)
        assert rubric is not None
        assert [c.text for c in rubric.criteria.all()] == [
            "the plan is the single home for the AC list",
            "a PASS grade cites the test that proves it",
        ]

    def test_a_reasoned_negative_writes_no_rubric(self) -> None:
        ticket = _ticket()
        manifest = _full_adequacy()
        manifest["acceptance_criteria"] = {"none_reason": "docs-only change, no behaviour to accept"}
        PlanArtifact.record(
            ticket=ticket, plan_text="real plan", recorded_by="op", base_sha=_FORTY_HEX, adequacy=manifest
        )
        assert Rubric.objects.active_for_ticket(ticket) is None

    def test_re_recording_the_manifest_keeps_the_verifiers_grade(self) -> None:
        # `plan-reaffirm` re-records the SAME manifest against a new base. A replace
        # would reset every grade, silently discarding a verifier's work.
        ticket = _ticket()
        manifest = _full_adequacy()
        PlanArtifact.record(
            ticket=ticket, plan_text="real plan", recorded_by="op", base_sha=_FORTY_HEX, adequacy=manifest
        )
        rubric = Rubric.objects.active_for_ticket(ticket)
        assert rubric is not None
        rubric.criteria.get(ordinal=0).record_grade(
            status="pass", grader_identity="cold-reviewer", reviewed_sha=_FORTY_HEX, rationale=_CITATION
        )
        before = list(rubric.criteria.values_list("text", flat=True))

        PlanArtifact.record(
            ticket=ticket, plan_text="reaffirmed plan", recorded_by="op", base_sha=_OTHER_HEX, adequacy=manifest
        )

        graded = rubric.criteria.get(ordinal=0)
        assert graded.status == RubricCriterion.Status.PASS
        assert graded.grader_identity == "cold-reviewer"
        assert graded.reviewed_sha == _FORTY_HEX
        assert list(rubric.criteria.values_list("text", flat=True)) == before

    def test_a_seeded_phase_criterion_survives_the_plan(self) -> None:
        # Entering `planning` seeds the phase's standing criterion, and the plan lands
        # after it — a replace would delete the standard the factory holds itself to.
        ticket = _ticket()
        Session.objects.create(ticket=ticket).visit_phase("planning")
        seeded = Rubric.objects.active_for_ticket(ticket)
        assert seeded is not None
        assert [c.text for c in seeded.criteria.all()] == [PHASE_CRITERIA["planning"]]

        PlanArtifact.record(
            ticket=ticket, plan_text="real plan", recorded_by="op", base_sha=_FORTY_HEX, adequacy=_full_adequacy()
        )

        rubric = Rubric.objects.active_for_ticket(ticket)
        assert rubric is not None
        criteria = list(rubric.criteria.all())
        assert criteria[0].ordinal == 0
        assert criteria[0].text == PHASE_CRITERIA["planning"]
        assert {c.text for c in criteria} == {
            PHASE_CRITERIA["planning"],
            *_full_adequacy()["acceptance_criteria"]["content"],
        }

    def test_an_unfalsifiable_criterion_refuses_the_whole_plan(self) -> None:
        # Rubric.add_criteria refuses a checklist satisfiable by inaction; it shares
        # record()'s atomic, so the plan row is rolled back with it.
        ticket = _ticket()
        manifest = _full_adequacy()
        manifest["acceptance_criteria"] = {"content": ["the existing resolver test suite passes UNMODIFIED"]}
        with pytest.raises(ValueError, match="satisfiable by INACTION"):
            PlanArtifact.record(
                ticket=ticket, plan_text="real plan", recorded_by="op", base_sha=_FORTY_HEX, adequacy=manifest
            )
        assert PlanArtifact.objects.filter(ticket=ticket).count() == 0
        assert Rubric.objects.active_for_ticket(ticket) is None


class TestBypassCarveOut(TestCase):
    def test_bypass_exempt_from_enforcement_records_all_negatives(self) -> None:
        ticket = _ticket()
        artifact = PlanArtifact.record_bypass(
            ticket=ticket, plan_text="[audited bypass by alice] urgent hotfix", recorded_by="alice"
        )
        assert artifact.adequacy == dict(all_negated_adequacy("[audited bypass by alice] urgent hotfix"))
        assert Rubric.objects.active_for_ticket(ticket) is None

    def test_bypass_still_requires_non_blank_text_and_author(self) -> None:
        with pytest.raises(ValueError, match="plan_text is required"):
            PlanArtifact.record_bypass(ticket=_ticket(), plan_text="  ", recorded_by="alice")


class TestBaselineValidationUnchanged(TestCase):
    """The pre-existing blank-text / blank-author refusals are preserved."""

    def test_blank_plan_text_still_refused(self) -> None:
        with pytest.raises(ValueError, match="plan_text is required"):
            PlanArtifact.record(ticket=_ticket(), plan_text="   ", recorded_by="op")

    def test_blank_author_still_refused(self) -> None:
        with pytest.raises(ValueError, match="recorded_by is required"):
            PlanArtifact.record(ticket=_ticket(), plan_text="plan", recorded_by="  ")
