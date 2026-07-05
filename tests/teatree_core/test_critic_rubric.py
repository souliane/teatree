"""The critic rubric (SELFCATCH-5): registry conformance + each predicate load-bearing.

Two guarantees. (1) The registry-walk conformance test resolves every item's
``predicate_path`` against the live module tree — a renamed/removed predicate fails
the build instead of going phantom (the ``chokepoint_registry`` drift-catch idiom).
(2) Each of the 8 seeded predicates has a CAUGHT fixture (a delivery exhibiting the
failure class this session's human had to point out) and a CLEAN twin — proving the
predicate is load-bearing, not vacuous. The mechanical predicates fire over REAL
production artifacts (PlanArtifact, MergeAudit, spec_coverage); the semantic ones
prove their justification field is load-bearing (declared-unjustified → caught,
declared-justified → clean, not merely absent-vs-present).
"""

import pytest
from django.test import TestCase

from teatree.core import critic_rubric
from teatree.core.critic_rubric import (
    CRITIC_RUBRIC,
    CriticRubricResolutionError,
    _resolve_predicate,
    coherence,
    completeness,
    deferred,
    done_not_done,
    duplication,
    ignored_input,
    rubric_items,
    spec_not_plan,
    unenforced_guarantee,
)
from teatree.core.models import MergeAudit, MergeClear, PlanArtifact, Ticket
from teatree.core.models.plan_adequacy import all_negated_adequacy

_FORTY_HEX = "a" * 40


def _adequate_manifest() -> dict:
    return dict(all_negated_adequacy("clean delivery"))


def _plan(ticket: Ticket, *, adequacy: dict | None) -> PlanArtifact:
    return PlanArtifact.objects.create(
        ticket=ticket,
        plan_text="plan body",
        recorded_by="planner",
        base_sha=_FORTY_HEX,
        adequacy=adequacy if adequacy is not None else {},
    )


def _merge_audit(ticket: Ticket) -> MergeAudit:
    clear = MergeClear.objects.create(
        ticket=ticket,
        pr_id=42,
        slug="souliane/teatree",
        reviewed_sha=_FORTY_HEX,
        reviewer_identity="cold-reviewer",
        gh_verify_result=MergeClear.VerifyResult.GREEN,
        blast_class=MergeClear.BlastClass.LOGIC,
    )
    return MergeAudit.objects.create(clear=clear, merged_sha=_FORTY_HEX, required_checks_status="green")


class TestRegistryConformance(TestCase):
    def test_every_predicate_path_resolves_to_a_callable(self) -> None:
        for item in rubric_items():
            assert callable(item.resolve()), item.slug

    def test_eight_seeded_items_with_unique_slugs(self) -> None:
        slugs = [item.slug for item in CRITIC_RUBRIC]
        assert len(slugs) == 8
        assert len(set(slugs)) == 8

    def test_every_item_carries_a_question_and_origin(self) -> None:
        for item in CRITIC_RUBRIC:
            assert item.adversarial_question.strip(), item.slug
            assert item.origin.strip(), item.slug

    def test_resolve_rejects_a_non_dotted_path(self) -> None:
        with pytest.raises(CriticRubricResolutionError):
            _resolve_predicate("notdotted")

    def test_resolve_rejects_a_non_callable_attr(self) -> None:
        with pytest.raises(CriticRubricResolutionError):
            _resolve_predicate("teatree.core.critic_rubric.CRITIC_RUBRIC")


class TestSpecNotPlanPredicate(TestCase):
    def test_caught_when_no_plan_artifact(self) -> None:
        ticket = Ticket.objects.create(overlay="t3-teatree", state=Ticket.State.RETROSPECTED)
        assert spec_not_plan(ticket)  # no PlanArtifact at all

    def test_caught_when_plan_manifest_is_thin(self) -> None:
        ticket = Ticket.objects.create(overlay="t3-teatree", state=Ticket.State.RETROSPECTED)
        _plan(ticket, adequacy={})  # a scope+acceptance thin spec — no four-section manifest
        assert spec_not_plan(ticket)

    def test_clean_with_an_adequate_manifest(self) -> None:
        ticket = Ticket.objects.create(overlay="t3-teatree", state=Ticket.State.RETROSPECTED)
        _plan(ticket, adequacy=_adequate_manifest())
        assert spec_not_plan(ticket) is None


class TestDoneNotDonePredicate(TestCase):
    def test_caught_when_no_merge_evidence(self) -> None:
        ticket = Ticket.objects.create(overlay="t3-teatree", state=Ticket.State.RETROSPECTED)
        assert done_not_done(ticket)  # no MergeAudit row, no PR to confirm merged

    def test_clean_with_a_keystone_merge_audit(self) -> None:
        ticket = Ticket.objects.create(overlay="t3-teatree", state=Ticket.State.RETROSPECTED)
        _merge_audit(ticket)
        assert done_not_done(ticket) is None


class TestCompletenessPredicate(TestCase):
    def test_caught_when_an_acceptance_criterion_is_unbacked(self) -> None:
        ticket = Ticket.objects.create(overlay="t3-teatree", state=Ticket.State.RETROSPECTED)
        ticket.extra = {
            "spec_coverage": {
                "acceptance_criteria": [
                    {"id": "AC-1", "tests": ["tests/test_a.py::t"]},
                    {"id": "AC-2", "tests": []},  # silently dropped
                ]
            }
        }
        assert completeness(ticket)

    def test_clean_when_every_criterion_is_backed(self) -> None:
        ticket = Ticket.objects.create(overlay="t3-teatree", state=Ticket.State.RETROSPECTED)
        ticket.extra = {"spec_coverage": {"acceptance_criteria": [{"id": "AC-1", "tests": ["tests/test_a.py::t"]}]}}
        assert completeness(ticket) is None


class TestCoherencePredicate(TestCase):
    def test_caught_when_a_concept_merge_has_no_rationale(self) -> None:
        ticket = Ticket.objects.create(overlay="t3-teatree", state=Ticket.State.RETROSPECTED)
        ticket.extra = {"critic": {"concept_merges": [{"merged": "companions into requires", "rationale": ""}]}}
        assert coherence(ticket)

    def test_clean_when_the_merge_cites_its_rationale(self) -> None:
        ticket = Ticket.objects.create(overlay="t3-teatree", state=Ticket.State.RETROSPECTED)
        ticket.extra = {
            "critic": {
                "concept_merges": [{"merged": "companions into requires", "rationale": "both are load-order deps"}]
            }
        }
        assert coherence(ticket) is None


class TestDuplicationPredicate(TestCase):
    def test_caught_when_a_new_impl_did_no_existing_search(self) -> None:
        ticket = Ticket.objects.create(overlay="t3-teatree", state=Ticket.State.RETROSPECTED)
        ticket.extra = {"critic": {"new_implementations": [{"symbol": "render_ref", "existing_search": ""}]}}
        assert duplication(ticket)

    def test_clean_when_the_new_impl_cites_its_search(self) -> None:
        ticket = Ticket.objects.create(overlay="t3-teatree", state=Ticket.State.RETROSPECTED)
        ticket.extra = {
            "critic": {"new_implementations": [{"symbol": "render_ref", "existing_search": "grep ref_render"}]}
        }
        assert duplication(ticket) is None


class TestDeferredPredicate(TestCase):
    def test_caught_when_a_deferral_has_no_filed_ticket(self) -> None:
        ticket = Ticket.objects.create(overlay="t3-teatree", state=Ticket.State.RETROSPECTED)
        ticket.extra = {"critic": {"deferrals": [{"what": "seam-parity checker", "ticket": ""}]}}
        assert deferred(ticket)

    def test_clean_when_the_deferral_names_its_ticket(self) -> None:
        ticket = Ticket.objects.create(overlay="t3-teatree", state=Ticket.State.RETROSPECTED)
        ticket.extra = {"critic": {"deferrals": [{"what": "seam-parity checker", "ticket": "souliane/teatree#123"}]}}
        assert deferred(ticket) is None


class TestIgnoredInputPredicate(TestCase):
    def test_caught_when_a_provided_input_is_unaddressed(self) -> None:
        ticket = Ticket.objects.create(overlay="t3-teatree", state=Ticket.State.RETROSPECTED)
        ticket.extra = {"provided_inputs": ["https://example.com/paste/abc"], "addressed_inputs": []}
        assert ignored_input(ticket)

    def test_clean_when_every_input_is_addressed(self) -> None:
        ticket = Ticket.objects.create(overlay="t3-teatree", state=Ticket.State.RETROSPECTED)
        ticket.extra = {
            "provided_inputs": ["https://example.com/paste/abc"],
            "addressed_inputs": ["https://example.com/paste/abc"],
        }
        assert ignored_input(ticket) is None


class TestUnenforcedGuaranteePredicate(TestCase):
    def test_caught_when_a_guarantee_cites_no_test(self) -> None:
        ticket = Ticket.objects.create(overlay="t3-teatree", state=Ticket.State.RETROSPECTED)
        ticket.extra = {"critic": {"guarantees": [{"claim": "never blocks", "test": ""}]}}
        assert unenforced_guarantee(ticket)

    def test_clean_when_the_guarantee_cites_its_test(self) -> None:
        ticket = Ticket.objects.create(overlay="t3-teatree", state=Ticket.State.RETROSPECTED)
        ticket.extra = {
            "critic": {"guarantees": [{"claim": "never blocks", "test": "tests/gates/test_critic_gate.py::t"}]}
        }
        assert unenforced_guarantee(ticket) is None


class TestModuleExportsEveryPredicateItReferences(TestCase):
    def test_no_registry_item_points_outside_the_module(self) -> None:
        # A predicate must live where the rubric says it does — the registry-walk
        # already resolves it, this pins the intended home so a stray move is loud.
        for item in CRITIC_RUBRIC:
            module, _, attr = item.predicate_path.rpartition(".")
            assert module == critic_rubric.__name__, item.slug
            assert hasattr(critic_rubric, attr), item.slug
