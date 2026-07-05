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
# The seeded registry — 3 deterministic (blocking) + 5 LLM (advisory).
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
)


def rubric_items() -> tuple[CriticRubricItem, ...]:
    """The active critic rubric, in seeded order."""
    return CRITIC_RUBRIC


def deterministic_items() -> tuple[CriticRubricItem, ...]:
    return tuple(item for item in CRITIC_RUBRIC if item.kind is RubricKind.DETERMINISTIC)


def llm_items() -> tuple[CriticRubricItem, ...]:
    return tuple(item for item in CRITIC_RUBRIC if item.kind is RubricKind.LLM)


def item_for(slug: str) -> "CriticRubricItem | None":
    return next((item for item in CRITIC_RUBRIC if item.slug == slug), None)
