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

from teatree.core.gates.critic_gate import build_critic_contract
from teatree.core.gates.design_critic_gate import build_design_contract
from teatree.core.gates.merge_quality_gate import build_merge_quality_contract
from teatree.core.models import MergeAudit, MergeClear, PlanArtifact, Ticket
from teatree.core.models.plan_adequacy import all_negated_adequacy
from teatree.core.review import critic_rubric
from teatree.core.review.critic_rubric import (
    CRITIC_RUBRIC,
    CriticRubricItem,
    CriticRubricResolutionError,
    RubricKind,
    _resolve_predicate,
    completeness,
    deterministic_items,
    done_not_done,
    item_for,
    llm_items,
    rubric_items,
    spec_not_plan,
)

_FORTY_HEX = "a" * 40

#: The north-star PR-5 design critic's four ``transition="plan"`` LLM items, in seeded order.
_PLAN_DESIGN_SLUG_ORDER = ("generality", "sketch_conformance", "convention_fit", "refactor_honesty")
_PLAN_DESIGN_SLUGS = set(_PLAN_DESIGN_SLUG_ORDER)


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
    def test_eight_seeded_mark_delivered_items_with_globally_unique_slugs(self) -> None:
        # The 8 seeded classes live at mark_delivered; the north-star transitions
        # append to the SAME registry, so assert the seeded count on the transition
        # subset and every slug's global uniqueness (a merge/plan item can never
        # collide with a seeded one).
        seeded = rubric_items("mark_delivered")
        assert len(seeded) == 8
        assert len({item.slug for item in seeded}) == 8
        all_slugs = [item.slug for item in CRITIC_RUBRIC]
        assert len(set(all_slugs)) == len(all_slugs)

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
            _resolve_predicate("teatree.core.review.critic_rubric.CRITIC_RUBRIC")


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

    def test_rubric_items_returns_the_mark_delivered_subset(self) -> None:
        # The seeded registry all lives at mark_delivered, so the default-transition
        # accessor is exactly those items — the merge pair is selected by its own transition.
        assert rubric_items("mark_delivered") == tuple(i for i in CRITIC_RUBRIC if i.transition == "mark_delivered")


class TestTransitionSelection:
    """Accessors return only the items keyed to the requested transition (the PR-1 seam)."""

    _PLAN_ITEM = CriticRubricItem(
        slug="plan_only_probe",
        adversarial_question="a plan-transition item that must not leak into mark_delivered",
        kind=RubricKind.DETERMINISTIC,
        origin="north-star plan critic",
        predicate_path="teatree.core.review.critic_rubric.spec_not_plan",
        blocking=True,
        transition="plan",
    )

    def _mixed_rubric(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(critic_rubric, "CRITIC_RUBRIC", (*CRITIC_RUBRIC, self._PLAN_ITEM))

    def test_seeded_items_are_keyed_to_mark_delivered(self) -> None:
        assert {item.slug for item in rubric_items("mark_delivered")} == {
            "spec_not_plan",
            "done_not_done",
            "completeness",
            "coherence",
            "duplication",
            "deferred",
            "ignored_input",
            "unenforced_guarantee",
        }

    def test_deterministic_items_excludes_a_foreign_transition(self, monkeypatch: pytest.MonkeyPatch) -> None:
        self._mixed_rubric(monkeypatch)
        slugs = {item.slug for item in deterministic_items("mark_delivered")}
        assert slugs == {"spec_not_plan", "done_not_done", "completeness"}
        assert "plan_only_probe" not in slugs

    def test_deterministic_items_selects_the_requested_transition(self, monkeypatch: pytest.MonkeyPatch) -> None:
        self._mixed_rubric(monkeypatch)
        assert [item.slug for item in deterministic_items("plan")] == ["plan_only_probe"]

    def test_llm_and_rubric_items_are_transition_scoped(self, monkeypatch: pytest.MonkeyPatch) -> None:
        self._mixed_rubric(monkeypatch)
        assert len(llm_items("mark_delivered")) == 5
        # PR-5's four design items are the LLM items at transition="plan"; the probe is deterministic.
        assert {item.slug for item in llm_items("plan")} == _PLAN_DESIGN_SLUGS
        assert len(rubric_items("mark_delivered")) == 8
        assert [item.slug for item in rubric_items("plan")] == [*_PLAN_DESIGN_SLUG_ORDER, "plan_only_probe"]

    def test_item_for_is_scoped_to_its_transition(self, monkeypatch: pytest.MonkeyPatch) -> None:
        self._mixed_rubric(monkeypatch)
        assert item_for("plan_only_probe", "plan") is self._PLAN_ITEM
        assert item_for("plan_only_probe", "mark_delivered") is None
        assert item_for("spec_not_plan", "mark_delivered") is not None


class TestMergeTransitionItems(TestCase):
    """The north-star PR-4 merge-quality pair (``test_value`` + ``cleanliness``) at ``transition="merge"``.

    They are LLM advisory items on the SAME registry, selected by transition — so
    the mark_delivered critic never sees them (proven here + in the gate tests) and
    the merge gate never sees the seeded eight.
    """

    def test_two_merge_llm_items(self) -> None:
        merge = llm_items("merge")
        assert {item.slug for item in merge} == {"test_value", "cleanliness"}
        assert all(item.kind is RubricKind.LLM and not item.blocking and item.transition == "merge" for item in merge)

    def test_merge_items_do_not_leak_into_mark_delivered(self) -> None:
        seeded = {item.slug for item in rubric_items("mark_delivered")}
        assert "test_value" not in seeded
        assert "cleanliness" not in seeded
        assert deterministic_items("merge") == ()  # both merge items are LLM-advisory, none deterministic-blocking

    def test_every_merge_llm_item_is_asked_by_the_merge_contract(self) -> None:
        # A merge item the critic prompt forgets would never get judged — pin that the
        # merge contract asks for every merge slug (production-shaped, over a real ticket).
        ticket = Ticket.objects.create(overlay="t3-teatree", state=Ticket.State.IN_REVIEW)
        contract = build_merge_quality_contract(ticket, _FORTY_HEX)
        for item in llm_items("merge"):
            assert item.slug in contract, item.slug


class TestPlanTransitionItems(TestCase):
    """The north-star PR-5 design pair-of-pairs at ``transition="plan"`` — the generic-vs-hack judgment.

    Four LLM advisory items on the SAME registry, selected by transition — the
    mark_delivered and merge critics never see them, and the design critic never sees
    the seeded eight or the merge pair.
    """

    def test_four_plan_llm_items(self) -> None:
        plan = llm_items("plan")
        assert {item.slug for item in plan} == _PLAN_DESIGN_SLUGS
        assert all(item.kind is RubricKind.LLM and not item.blocking and item.transition == "plan" for item in plan)

    def test_plan_items_do_not_leak_into_other_transitions(self) -> None:
        for other in ("mark_delivered", "merge"):
            other_slugs = {item.slug for item in rubric_items(other)}
            assert not (_PLAN_DESIGN_SLUGS & other_slugs), other
        assert deterministic_items("plan") == ()  # all four design items are LLM-advisory, none deterministic

    def test_every_plan_llm_item_is_asked_by_the_design_contract(self) -> None:
        # A design item the critic prompt forgets would never get judged — pin that the
        # design contract asks for every plan slug (production-shaped, over a real ticket).
        ticket = Ticket.objects.create(overlay="t3-teatree", state=Ticket.State.PLANNED)
        contract = build_design_contract(ticket, _FORTY_HEX)
        for item in llm_items("plan"):
            assert item.slug in contract, item.slug
