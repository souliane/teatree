"""The autonomous user-proxy critic's rubric (SELFCATCH-5) — the 8 seeded classes.

Each :class:`CriticRubricItem` is one adversarial question the human had to ask
this session, turned into a checkable verdict over the ticket's DELIVERED
artifacts. The invariant the whole layer delivers: each class is human-caught AT
MOST ONCE, then promoted here and caught upstream forever.

Two kinds, honestly distinguished — do NOT claim every item is a deterministic
predicate over delivered artifacts:

DETERMINISTIC (the blocking teeth)
    ``spec_not_plan``, ``done_not_done``, ``completeness`` — a pure predicate over
    REAL artifacts (PlanArtifact adequacy, keystone MergeAudit + worktree state,
    the spec_coverage manifest). Each REUSES its sibling gate rather than
    re-implementing it, and fires on ABSENCE so an empty delivery cannot wave it
    through. These are the items that BLOCK when enforcement is live — no LLM in
    the blocking path.

LLM (the semantic net, advisory)
    ``coherence``, ``duplication``, ``deferred``, ``ignored_input``,
    ``unenforced_guarantee`` — determinism cannot read them, so the async critic
    (:class:`~teatree.core.models.critic_dispatch.CriticDispatch`) judges them
    against the REAL delivered artifacts (plan text, the diff's changed files, the
    intake attachment manifest) and returns a
    :class:`~teatree.core.models.critic_verdict.CriticVerdict`. They are ADVISORY —
    the gate records findings from the verdict, never blocks on it — and they do
    NOT read any self-declared ``extra`` key that no producer writes.

The registry is the frozen-dataclass + dotted-path-resolve + registry-walk-test
idiom of :mod:`teatree.core.chokepoint_registry`: a conformance test resolves every
DETERMINISTIC item's ``predicate_path`` and asserts every LLM item's slug is one the
critic dispatch prompt actually asks for — a renamed predicate or an LLM item the
prompt forgets fails the build instead of going phantom.
"""

import importlib
from collections.abc import Callable
from dataclasses import dataclass
from enum import StrEnum
from typing import TYPE_CHECKING

from teatree.core.gates.merge_evidence_gate import has_merge_evidence
from teatree.core.gates.plan_currency_gate import latest_plan_artifact
from teatree.core.gates.spec_coverage_gate import acceptance_criteria, override_reason, uncovered_acs
from teatree.core.models.plan_adequacy import is_adequate
from teatree.core.models.ticket_worktree_checks import collect_dirty_worktree_paths

if TYPE_CHECKING:
    from teatree.core.models.ticket import Ticket

CriticPredicate = Callable[["Ticket"], "str | None"]

# The FSM transition an item is judged at. The seeded rubric all lands here (the
# critic gate runs at ``mark_delivered``); the north-star arc adds ``plan``/``merge``
# items on the SAME registry, selected by transition — keeping the models
# (CriticDispatch/CriticVerdict/CriticFinding), which are already transition-keyed,
# and the rubric in step.
DEFAULT_TRANSITION = "mark_delivered"

# The merge-quality critic's transition (north-star PR-4): ``test_value`` +
# ``cleanliness`` are judged at ship and gate ``execute_bound_merge`` for
# directive tickets — a merely-green-but-not-well-engineered change is refused.
_MERGE_TRANSITION = "merge"

# The design critic's transition (north-star PR-5): the generic-vs-hack judgment at
# PLAN time. The four items judge the ratified MechanismSketch/plan for what
# determinism can't — the LLM half of the anti-hack teeth, the deterministic
# ``mechanism_placement`` adequacy section catches the structural shape.
_PLAN_TRANSITION = "plan"


class RubricKind(StrEnum):
    DETERMINISTIC = "deterministic"
    LLM = "llm"


class CriticRubricResolutionError(ValueError):
    """A rubric item's ``predicate_path`` did not resolve to a callable."""


def _resolve_predicate(path: str) -> CriticPredicate:
    module_path, _, attr = path.rpartition(".")
    if not module_path or not attr:
        msg = f"critic predicate path {path!r} is not a dotted module.attr reference"
        raise CriticRubricResolutionError(msg)
    try:
        module = importlib.import_module(module_path)
    except ImportError as exc:
        msg = f"critic predicate path {path!r} names a module that does not import: {exc}"
        raise CriticRubricResolutionError(msg) from exc
    resolved = getattr(module, attr, None)
    if not callable(resolved):
        msg = f"critic predicate path {path!r} does not resolve to a callable"
        raise CriticRubricResolutionError(msg)
    return resolved


@dataclass(frozen=True, slots=True)
class CriticRubricItem:
    """One rubric item: the adversarial question + how its verdict is produced.

    ``kind`` DETERMINISTIC → ``predicate_path`` names a pure predicate over real
    artifacts and ``blocking`` decides whether a FAIL blocks under enforcement.
    ``kind`` LLM → ``predicate_path`` is empty; the async critic judges it and the
    item is always advisory.
    """

    slug: str
    adversarial_question: str
    kind: RubricKind
    origin: str
    predicate_path: str = ""
    blocking: bool = False
    transition: str = DEFAULT_TRANSITION

    def resolve(self) -> CriticPredicate:
        return _resolve_predicate(self.predicate_path)

    def evaluate(self, ticket: "Ticket") -> "str | None":
        return self.resolve()(ticket)


# --------------------------------------------------------------------------- #
# Deterministic predicates — REUSE the sibling gates, fire on absence.
# --------------------------------------------------------------------------- #
def spec_not_plan(ticket: "Ticket") -> "str | None":
    """Is the plan a real plan, or a thin scope+acceptance spec authored against a stale base?"""
    artifact = latest_plan_artifact(ticket)
    if artifact is None:
        return "delivered with no PlanArtifact — no plan bound the work at all"
    if not is_adequate(artifact.adequacy):
        return (
            "the governing plan has no adequate four-section manifest (design, integration_seams, "
            "edge_cases, test_strategy) — a thin/underspecified spec passed as a plan"
        )
    return None


def done_not_done(ticket: "Ticket") -> "str | None":
    """Is this actually DONE — merged with a real SHA, and the worktree clean?"""
    if not has_merge_evidence(ticket):
        return (
            "marked delivered with no merged-SHA evidence (no keystone MergeAudit row, forge does not "
            "confirm merged) — believe-done-not-done: committed/tested is not merged"
        )
    dirty = collect_dirty_worktree_paths(ticket)
    if dirty:
        return f"delivered with uncommitted tracked changes still in the worktree(s): {', '.join(dirty)}"
    return None


def completeness(ticket: "Ticket") -> "str | None":
    """Is every acceptance criterion delivered, or was the scope silently reduced to a subset?

    Matches the real ``check_spec_coverage`` gate: a recorded ``spec_coverage_override``
    reason passes; a MISSING manifest is itself a FAIL (declaring done on zero proven
    ACs is the partial-subset claim), not a pass-clean; an uncovered AC is a FAIL.
    """
    if override_reason(ticket):
        return None
    if not acceptance_criteria(ticket):
        return (
            "delivered with no spec-coverage manifest — zero acceptance criteria proven is the "
            "partial-subset claim done cannot be declared on (record extra['spec_coverage'] or an override)"
        )
    uncovered = uncovered_acs(ticket)
    if uncovered:
        return f"{len(uncovered)} acceptance criterion(s) have no backing test — scope reduced: {', '.join(uncovered)}"
    return None


# --------------------------------------------------------------------------- #
# The registry — 8 seeded ``mark_delivered`` items (3 deterministic blocking + 5
# LLM advisory) plus the north-star ``merge`` LLM pair (``test_value`` +
# ``cleanliness``). Accessors below select by transition, so the mark_delivered
# critic never sees the merge items and vice versa.
# --------------------------------------------------------------------------- #
CRITIC_RUBRIC: tuple[CriticRubricItem, ...] = (
    CriticRubricItem(
        slug="spec_not_plan",
        adversarial_question="Is this a real plan naming its files and seams, or a thin spec on a stale base?",
        kind=RubricKind.DETERMINISTIC,
        predicate_path="teatree.core.critic_rubric.spec_not_plan",
        blocking=True,
        origin="thin/underspecified plan; stale-base-not-rebased",
    ),
    CriticRubricItem(
        slug="done_not_done",
        adversarial_question="Is this actually done — merged with a real SHA — or just committed and believed done?",
        kind=RubricKind.DETERMINISTIC,
        predicate_path="teatree.core.critic_rubric.done_not_done",
        blocking=True,
        origin="believe-done-not-done",
    ),
    CriticRubricItem(
        slug="completeness",
        adversarial_question="Is every acceptance criterion delivered, or was the scope silently reduced to a subset?",
        kind=RubricKind.DETERMINISTIC,
        predicate_path="teatree.core.critic_rubric.completeness",
        blocking=True,
        origin="silent scope reduction",
    ),
    CriticRubricItem(
        slug="coherence",
        adversarial_question="Does any merged/renamed concept conflate two things that serve different intents?",
        kind=RubricKind.LLM,
        origin="concept-conflation (companions vs requires)",
    ),
    CriticRubricItem(
        slug="duplication",
        adversarial_question="Was an existing implementation searched for before this new one was written?",
        kind=RubricKind.LLM,
        origin="new implementation without a duplication check",
    ),
    CriticRubricItem(
        slug="deferred",
        adversarial_question="Is every 'deferred by design' backed by a filed ticket, or is it bare prose?",
        kind=RubricKind.LLM,
        origin="TODO-list-not-reconciled-with-reality; deferred-by-prose",
    ),
    CriticRubricItem(
        slug="ignored_input",
        adversarial_question="Was every user-provided URL/attachment/directive addressed or explicitly declined?",
        kind=RubricKind.LLM,
        origin="ignored user-provided context/paste",
    ),
    CriticRubricItem(
        slug="unenforced_guarantee",
        adversarial_question="Does every asserted invariant (never/always) cite the test or gate that enforces it?",
        kind=RubricKind.LLM,
        origin="missing anti-vacuity / vacuous-green",
    ),
    # The merge-quality critic (north-star PR-4): two LLM items judged at
    # ``transition="merge"`` (the same registry, selected by transition — PR-1's
    # seam). They make "clean + tested-enough WITHOUT bloat" a real merge gate: a
    # verdict covering the shipped head must carry zero FAILs before
    # ``execute_bound_merge`` lets a directive keystone through. The full judging
    # rubric — the ratified ``test_strategy`` anchor, the both-directions
    # anti-vacuity/anti-bloat wording, the CLAUDE.md cleanliness bar — lives in
    # ``merge_quality_gate.build_merge_quality_contract`` (kept out of the 255-char
    # ``adversarial_question`` a finding stores).
    CriticRubricItem(
        slug="test_value",
        adversarial_question=(
            "Does each added test assert real behavior that could fail (not vacuous), with the "
            "coverage-that-matters present and no redundant/bloat tests, anchored to the ratified test_strategy?"
        ),
        kind=RubricKind.LLM,
        origin="vacuous-green / test-bloat (merely-green is not tested-enough)",
        transition=_MERGE_TRANSITION,
    ),
    CriticRubricItem(
        slug="cleanliness",
        adversarial_question=(
            "Does the change meet the CLAUDE.md bar — full typing, composition over inheritance, "
            "Django conventions, self-documenting names, docs/BLUEPRINT aligned?"
        ),
        kind=RubricKind.LLM,
        origin="green-but-not-clean (merely-green is not well-engineered)",
        transition=_MERGE_TRANSITION,
    ),
    # The design critic (north-star PR-5): four LLM items judged at
    # ``transition="plan"`` for directive tickets — the generic-vs-hack judgment the
    # deterministic ``mechanism_placement`` section can't make. Advisory-first behind
    # the ``design_critic_live`` DARK flag; the deterministic ``mechanism_conforms``
    # section is the blocking teeth, these are the semantic net. The full judging
    # rubric (the ratified sketch, the N=2 litmus) lives in
    # ``design_critic_gate.build_design_contract``.
    CriticRubricItem(
        slug="generality",
        adversarial_question=(
            "Would a second overlay wanting a different value need any code change? Is the mechanism a core "
            "setting + policy at the seam every overlay flows through, or a special-case one-off?"
        ),
        kind=RubricKind.LLM,
        origin="overlay-local one-off instead of a generic core mechanism (fails the N=2 litmus)",
        transition=_PLAN_TRANSITION,
    ),
    CriticRubricItem(
        slug="sketch_conformance",
        adversarial_question=(
            "Does the plan implement the ratified sketch and nothing unratified? Name any semantic drift "
            "the deterministic field-equality check can't see."
        ),
        kind=RubricKind.LLM,
        origin="plan drifts from the ratified sketch in spirit",
        transition=_PLAN_TRANSITION,
    ),
    CriticRubricItem(
        slug="convention_fit",
        adversarial_question=(
            "Does it follow the substrate conventions — settings recipe, flag-vs-setting discrimination, "
            "gate-registry idiom, tests-mirror-src — or invent a parallel mechanism?"
        ),
        kind=RubricKind.LLM,
        origin="parallel mechanism instead of the substrate convention",
        transition=_PLAN_TRANSITION,
    ),
    CriticRubricItem(
        slug="refactor_honesty",
        adversarial_question=(
            "Are the refactors the seam needs named and sequenced, or is the plan bolting onto a mess silently?"
        ),
        kind=RubricKind.LLM,
        origin="silent bolt-on onto an unrefactored seam",
        transition=_PLAN_TRANSITION,
    ),
)


def rubric_items(transition: str = DEFAULT_TRANSITION) -> tuple[CriticRubricItem, ...]:
    """The active critic rubric for *transition*, in seeded order."""
    return tuple(item for item in CRITIC_RUBRIC if item.transition == transition)


def deterministic_items(transition: str = DEFAULT_TRANSITION) -> tuple[CriticRubricItem, ...]:
    return tuple(
        item for item in CRITIC_RUBRIC if item.transition == transition and item.kind is RubricKind.DETERMINISTIC
    )


def llm_items(transition: str = DEFAULT_TRANSITION) -> tuple[CriticRubricItem, ...]:
    return tuple(item for item in CRITIC_RUBRIC if item.transition == transition and item.kind is RubricKind.LLM)


def item_for(slug: str, transition: str = DEFAULT_TRANSITION) -> "CriticRubricItem | None":
    return next((item for item in CRITIC_RUBRIC if item.slug == slug and item.transition == transition), None)
