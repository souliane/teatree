"""The critic rubric (SELFCATCH-5): registry conformance + each deterministic predicate load-bearing.

Conformance: every DETERMINISTIC item's ``predicate_path`` resolves, and every LLM
item's slug is one the dispatch contract actually asks the critic to judge — a
renamed predicate or a forgotten LLM item fails the build. Each of the 3
deterministic predicates has a CAUGHT fixture (over REAL production artifacts:
PlanArtifact adequacy, keystone MergeAudit, the spec_coverage manifest) and a CLEAN
twin, proving the predicate is load-bearing. The LLM items carry no predicate — they
are judged by the async critic (covered in the gate tests), never by a self-declared
key, so there is no vacuous predicate to test here.
"""

import pytest
from django.test import TestCase

from teatree.core import critic_rubric
from teatree.core.critic_rubric import (
    CRITIC_RUBRIC,
    CriticRubricResolutionError,
    RubricKind,
    _resolve_predicate,
    completeness,
    deterministic_items,
    done_not_done,
    llm_items,
    rubric_items,
    spec_not_plan,
)
from teatree.core.gates.critic_gate import build_critic_contract
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
    def test_eight_seeded_items_with_unique_slugs(self) -> None:
        slugs = [item.slug for item in CRITIC_RUBRIC]
        assert len(slugs) == 8
        assert len(set(slugs)) == 8

    def test_three_deterministic_blocking_five_llm_advisory(self) -> None:
        deterministic = deterministic_items()
        llm = llm_items()
        assert {i.slug for i in deterministic} == {"spec_not_plan", "done_not_done", "completeness"}
        assert all(i.blocking for i in deterministic)
        assert len(llm) == 5
        assert all(i.kind is RubricKind.LLM and not i.blocking and i.predicate_path == "" for i in llm)

    def test_every_deterministic_predicate_path_resolves(self) -> None:
        for item in deterministic_items():
            assert callable(item.resolve()), item.slug

    def test_every_llm_item_is_asked_by_the_dispatch_contract(self) -> None:
        # A LLM item the critic prompt forgets would never get judged — pin that the
        # contract asks for every LLM slug (production-shaped, over a real ticket).
        ticket = Ticket.objects.create(overlay="t3-teatree", state=Ticket.State.RETROSPECTED)
        contract = build_critic_contract(ticket, _FORTY_HEX)
        for item in llm_items():
            assert item.slug in contract, item.slug

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
        assert spec_not_plan(ticket)

    def test_caught_when_plan_manifest_is_thin(self) -> None:
        ticket = Ticket.objects.create(overlay="t3-teatree", state=Ticket.State.RETROSPECTED)
        _plan(ticket, adequacy={})
        assert spec_not_plan(ticket)

    def test_clean_with_an_adequate_manifest(self) -> None:
        ticket = Ticket.objects.create(overlay="t3-teatree", state=Ticket.State.RETROSPECTED)
        _plan(ticket, adequacy=_adequate_manifest())
        assert spec_not_plan(ticket) is None


class TestDoneNotDonePredicate(TestCase):
    def test_caught_when_no_merge_evidence(self) -> None:
        ticket = Ticket.objects.create(overlay="t3-teatree", state=Ticket.State.RETROSPECTED)
        assert done_not_done(ticket)

    def test_clean_with_a_keystone_merge_audit(self) -> None:
        ticket = Ticket.objects.create(overlay="t3-teatree", state=Ticket.State.RETROSPECTED)
        _merge_audit(ticket)
        assert done_not_done(ticket) is None


class TestCompletenessPredicate(TestCase):
    def test_caught_when_no_spec_coverage_manifest(self) -> None:
        # The no-manifest hole fix: zero proven ACs is a FAIL (matches check_spec_coverage), not pass-clean.
        ticket = Ticket.objects.create(overlay="t3-teatree", state=Ticket.State.RETROSPECTED)
        assert completeness(ticket)

    def test_caught_when_an_acceptance_criterion_is_unbacked(self) -> None:
        ticket = Ticket.objects.create(overlay="t3-teatree", state=Ticket.State.RETROSPECTED)
        ticket.extra = {
            "spec_coverage": {
                "acceptance_criteria": [
                    {"id": "AC-1", "tests": ["tests/test_a.py::t"]},
                    {"id": "AC-2", "tests": []},
                ]
            }
        }
        assert completeness(ticket)

    def test_clean_when_every_criterion_is_backed(self) -> None:
        ticket = Ticket.objects.create(overlay="t3-teatree", state=Ticket.State.RETROSPECTED)
        ticket.extra = {"spec_coverage": {"acceptance_criteria": [{"id": "AC-1", "tests": ["tests/test_a.py::t"]}]}}
        assert completeness(ticket) is None

    def test_clean_with_a_recorded_override(self) -> None:
        ticket = Ticket.objects.create(overlay="t3-teatree", state=Ticket.State.RETROSPECTED)
        ticket.extra = {"spec_coverage_override": {"reason": "pure docs change, no ACs"}}
        assert completeness(ticket) is None


class TestModuleExportsEveryDeterministicPredicate(TestCase):
    def test_no_deterministic_item_points_outside_the_module(self) -> None:
        for item in deterministic_items():
            module, _, attr = item.predicate_path.rpartition(".")
            assert module == critic_rubric.__name__, item.slug
            assert hasattr(critic_rubric, attr), item.slug

    def test_rubric_items_returns_the_full_registry(self) -> None:
        assert rubric_items() == CRITIC_RUBRIC
