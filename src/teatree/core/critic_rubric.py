"""The autonomous user-proxy critic's rubric (SELFCATCH-5) — the 8 seeded classes.

Each :class:`CriticRubricItem` is one adversarial question the human had to ask
this session, turned into a concrete deterministic predicate over the ticket's
DELIVERED artifacts. The critic gate (:mod:`teatree.core.gates.critic_gate`) walks
this registry at ``mark_delivered`` and records a
:class:`~teatree.core.models.critic_finding.CriticFinding` for every item whose
predicate CATCHES its failure class. The invariant the whole layer delivers: each
class is human-caught AT MOST ONCE, then promoted here and caught upstream forever.

The registry is the frozen-dataclass + dotted-path-resolve + registry-walk-test
idiom of :mod:`teatree.core.chokepoint_registry`: pure data, and a conformance test
(``tests/teatree_core/test_critic_rubric.py``) resolves every ``predicate_path`` so
a renamed/removed predicate fails the build rather than silently going phantom.

Predicate contract
    ``predicate(ticket) -> str | None`` — a NON-EMPTY detail string when the
    failure class is PRESENT (a finding), ``None`` when the item is clean. The
    detail names the offending artifact so the finding is dispatchable. A
    predicate that RAISES is inconclusive: the gate records an
    ``instrumentation_gap`` (counted as a FAIL, never a silent pass — the plan's
    anti-theater doctrine).

Determinism first
    The MECHANICAL classes (``spec_not_plan``, ``done_not_done``, ``completeness``)
    REUSE the sibling gates' checks — they never re-implement merge-evidence or
    plan-adequacy, they call them — and fire on ABSENCE (no plan / no merged-SHA /
    an uncovered criterion) so an empty delivery cannot wave them through. The
    SEMANTIC classes (``coherence``, ``duplication``, ``deferred``,
    ``ignored_input``, ``unenforced_guarantee``) enforce the plan's
    silence-never-passes doctrine over the delivery's DECLARED claims: a declared
    concept-merge / new-implementation / deferral / guarantee that omits its
    justification is caught. Detecting an UNDECLARED conflation is the deferred LLM
    critic's job; these predicates make an under-justified declaration louder than
    an honest one.
"""

import importlib
from collections.abc import Callable
from dataclasses import dataclass
from typing import TYPE_CHECKING

from teatree.core.gates.merge_evidence_gate import has_merge_evidence
from teatree.core.gates.plan_currency_gate import latest_plan_artifact
from teatree.core.gates.spec_coverage_gate import uncovered_acs
from teatree.core.models.plan_adequacy import is_adequate
from teatree.core.models.ticket_worktree_checks import collect_dirty_worktree_paths

if TYPE_CHECKING:
    from teatree.core.models.ticket import Ticket

CriticPredicate = Callable[["Ticket"], "str | None"]


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
    """One rubric item: the adversarial question + the predicate that decides it."""

    slug: str
    adversarial_question: str
    predicate_path: str
    origin: str

    def resolve(self) -> CriticPredicate:
        return _resolve_predicate(self.predicate_path)

    def evaluate(self, ticket: "Ticket") -> "str | None":
        return self.resolve()(ticket)


# --------------------------------------------------------------------------- #
# Shared readers over the delivery's declared critic claims.
# --------------------------------------------------------------------------- #
def _critic_claims(ticket: "Ticket", key: str) -> list[dict]:
    """The declared ``ticket.extra['critic'][<key>]`` list of claim mappings, or []."""
    manifest = (ticket.extra or {}).get("critic")
    if not isinstance(manifest, dict):
        return []
    claims = manifest.get(key)
    if not isinstance(claims, list):
        return []
    return [claim for claim in claims if isinstance(claim, dict)]


def _blank(value: object) -> bool:
    return not (isinstance(value, str) and value.strip())


# --------------------------------------------------------------------------- #
# Mechanical predicates — REUSE the sibling gates, fire on absence.
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
    """Is every acceptance criterion delivered, or was the scope silently reduced to a subset?"""
    uncovered = uncovered_acs(ticket)
    if uncovered:
        return (
            f"{len(uncovered)} acceptance criterion(s) have no backing artifact — scope silently "
            f"reduced to a subset: {', '.join(uncovered)}"
        )
    return None


# --------------------------------------------------------------------------- #
# Semantic predicates — silence-never-passes over the delivery's declared claims.
# --------------------------------------------------------------------------- #
def coherence(ticket: "Ticket") -> "str | None":
    """Any merged/renamed concept must cite the two concepts AND why they are one."""
    for claim in _critic_claims(ticket, "concept_merges"):
        if _blank(claim.get("rationale")):
            merged = str(claim.get("merged") or claim.get("concepts") or "<unnamed merge>")
            return f"concept-merge {merged!r} declared with no rationale — two distinct concepts may be conflated"
    return None


def duplication(ticket: "Ticket") -> "str | None":
    """Was an existing implementation searched for before a new one was accepted?"""
    for claim in _critic_claims(ticket, "new_implementations"):
        if _blank(claim.get("existing_search")):
            symbol = str(claim.get("symbol") or "<unnamed symbol>")
            return (
                f"new implementation {symbol!r} declared with no existing-implementation search — possible duplication"
            )
    return None


def deferred(ticket: "Ticket") -> "str | None":
    """Every 'deferred by design' claim needs a filed ticket — never bare prose."""
    for claim in _critic_claims(ticket, "deferrals"):
        if _blank(claim.get("ticket")):
            what = str(claim.get("what") or "<unnamed deferral>")
            return f"deferral {what!r} declared with no filed ticket — deferred-by-prose, not reconciled with reality"
    return None


def ignored_input(ticket: "Ticket") -> "str | None":
    """Every user-provided URL/attachment/directive must be addressed or explicitly declined."""
    extra = ticket.extra or {}
    provided = extra.get("provided_inputs")
    if not isinstance(provided, list):
        return None
    addressed_raw = extra.get("addressed_inputs")
    addressed = {str(item).strip() for item in addressed_raw} if isinstance(addressed_raw, list) else set()
    unaddressed = [str(item).strip() for item in provided if str(item).strip() and str(item).strip() not in addressed]
    if unaddressed:
        return f"user-provided input(s) neither addressed nor declined: {', '.join(unaddressed)}"
    return None


def unenforced_guarantee(ticket: "Ticket") -> "str | None":
    """Any docstring asserting an invariant (never/always) must cite the test/gate enforcing it."""
    for claim in _critic_claims(ticket, "guarantees"):
        if _blank(claim.get("test")):
            asserted = str(claim.get("claim") or "<unnamed guarantee>")
            return f"guarantee {asserted!r} asserted with no citing test — an unenforced/vacuous-green invariant"
    return None


# --------------------------------------------------------------------------- #
# The seeded registry — the 8 classes the human had to point out this session.
# --------------------------------------------------------------------------- #
CRITIC_RUBRIC: tuple[CriticRubricItem, ...] = (
    CriticRubricItem(
        slug="spec_not_plan",
        adversarial_question="Is this a real plan naming its files and seams, or a thin spec on a stale base?",
        predicate_path="teatree.core.critic_rubric.spec_not_plan",
        origin="thin/underspecified plan; stale-base-not-rebased",
    ),
    CriticRubricItem(
        slug="done_not_done",
        adversarial_question="Is this actually done — merged with a real SHA — or just committed and believed done?",
        predicate_path="teatree.core.critic_rubric.done_not_done",
        origin="believe-done-not-done",
    ),
    CriticRubricItem(
        slug="completeness",
        adversarial_question="Is every acceptance criterion delivered, or was the scope silently reduced to a subset?",
        predicate_path="teatree.core.critic_rubric.completeness",
        origin="silent scope reduction",
    ),
    CriticRubricItem(
        slug="coherence",
        adversarial_question="Does any merged/renamed concept conflate two things that serve different intents?",
        predicate_path="teatree.core.critic_rubric.coherence",
        origin="concept-conflation (companions vs requires)",
    ),
    CriticRubricItem(
        slug="duplication",
        adversarial_question="Was an existing implementation searched for before this new one was written?",
        predicate_path="teatree.core.critic_rubric.duplication",
        origin="new implementation without a duplication check",
    ),
    CriticRubricItem(
        slug="deferred",
        adversarial_question="Is every 'deferred by design' backed by a filed ticket, or is it bare prose?",
        predicate_path="teatree.core.critic_rubric.deferred",
        origin="TODO-list-not-reconciled-with-reality; deferred-by-prose",
    ),
    CriticRubricItem(
        slug="ignored_input",
        adversarial_question="Was every user-provided URL/attachment/directive addressed or explicitly declined?",
        predicate_path="teatree.core.critic_rubric.ignored_input",
        origin="ignored user-provided context/paste",
    ),
    CriticRubricItem(
        slug="unenforced_guarantee",
        adversarial_question="Does every asserted invariant (never/always) cite the test or gate that enforces it?",
        predicate_path="teatree.core.critic_rubric.unenforced_guarantee",
        origin="missing anti-vacuity / vacuous-green",
    ),
)


def rubric_items() -> tuple[CriticRubricItem, ...]:
    """The active critic rubric, in seeded order."""
    return CRITIC_RUBRIC
