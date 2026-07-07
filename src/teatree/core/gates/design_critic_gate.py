"""The design critic (north-star PR-5) — the generic-vs-hack judgment at PLAN time.

The LLM half of the anti-tech-debt-at-design-time teeth: the deterministic
``plan_adequacy.mechanism_conforms`` section catches the structural shape (an
overlay-package chokepoint, a drifted setting), but the ratified sketch itself could
be wrong, or the plan could conform in letter yet not in spirit. Four
``transition="plan"`` LLM rubric items — ``generality`` (the N=2 litmus),
``sketch_conformance``, ``convention_fit``, ``refactor_honesty`` — judge exactly that,
on the shared ``critic_reviewing`` phase, recording a ``CriticVerdict(transition="plan")``
at the plan's base SHA. This module is the ADVISORY reader over that verdict, split
judging from gating in time (the critic-gate doctrine — no LLM in a blocking path):

The judge (async)
    a headless critic reads the ratified sketch + the plan and RETURNS a
    ``critic_verdict`` envelope; ``attempt_recorder`` records the ``CriticVerdict``
    server-side (maker≠checker), armed here when no covering verdict exists.

The reader (advisory)
    at plan time, each FAIL item is mirrored into a ``CriticFinding(transition="plan")``
    naming the offending design decision — visible to the planner's repair loop and at
    the directive's verify horizon. It NEVER blocks: the deterministic
    ``mechanism_conforms`` gate is the teeth; the design critic surfaces what
    determinism can't, advisory-first — armed by the ``directive_loop_enabled`` DARK flag.

Wired at :meth:`~teatree.core.models.ticket.Ticket.plan` (via the gate registry, no
model→gate up-edge) for directive tickets only; a strict no-op — one settings read —
while the flag is dark, so ordinary planning is untouched.
"""

import logging
from typing import TYPE_CHECKING

from teatree.config import get_effective_settings
from teatree.core.gates.plan_currency_gate import latest_plan_artifact
from teatree.core.modelkit.gate_registry import register_gate
from teatree.core.models.critic_dispatch import CriticDispatch
from teatree.core.models.critic_finding import CriticFinding, CriticFindingSpec
from teatree.core.models.critic_verdict import CriticVerdict
from teatree.core.models.directive import Directive
from teatree.core.review.critic_rubric import _PLAN_TRANSITION, item_for, llm_items

if TYPE_CHECKING:
    from teatree.core.models.mechanism_sketch import MechanismSketch
    from teatree.core.models.ticket import Ticket

logger = logging.getLogger(__name__)


def design_critic_armed(overlay_name: str | None) -> bool:
    """Whether the design critic runs for *overlay_name* (overlay -> global). DARK by default.

    #104: the advisory-only design critic carries no independent switch — it fires only
    for directive-linked tickets, so it is armed BY ``directive_loop_enabled`` (arming the
    directive loop is exactly the condition under which the plan-time critic has work).
    """
    return bool(get_effective_settings(overlay_name).directive_loop_enabled)


def plan_head_sha(ticket: "Ticket") -> str:
    """The plan's authored base SHA — the head the design verdict is pinned to, or ``""``."""
    plan = latest_plan_artifact(ticket)
    return (plan.base_sha or "").strip().lower() if plan is not None else ""


def build_design_contract(ticket: "Ticket", head_sha: str) -> str:
    """The dispatch contract for the headless design critic (the plan rubric + ratified sketch injected).

    Names the plan LLM items from the registry (a forgotten item would never be judged),
    puts the ratified sketch's generic-shape decision in front of the model, and instructs
    it to RETURN a ``critic_verdict`` envelope — the same shape the mark_delivered/merge
    critics return, judged at the shared ``critic_reviewing`` phase.
    """
    questions = "\n".join(f"  - {item.slug}: {item.adversarial_question}" for item in llm_items(_PLAN_TRANSITION))
    item_slugs = ", ".join(item.slug for item in llm_items(_PLAN_TRANSITION))
    sketch_block = _sketch_block(ticket)
    return (
        f"You are the DESIGN critic deciding whether the ratified mechanism + plan on directive ticket "
        f"{ticket.pk} at head {head_sha[:8] or '<unknown>'} is a clean GENERIC core mechanism, NOT a one-off "
        f"hack. Read the plan and the touched modules, then answer each item against the ARTIFACTS.\n\n"
        f"Ratified sketch (the generic-shape decision the human approved):\n{sketch_block}\n\n"
        f"A hack — a special-case in an overlay package, or a one-off a second overlay would have to change "
        f"code to reuse — FAILS. A core setting + a policy check at the seam every overlay flows through, "
        f"activated by data, PASSES. Judge the ratified shape too: a wrongly-ratified sketch is a `generality` "
        f"or `convention_fit` FAIL. Cite file:line (or the sketch field) for every verdict.\n\n"
        f"Questions:\n{questions}\n\nRETURN your judgment in the result envelope (the phase has no shell — the "
        f'orchestrator records it): `"critic_verdict": {{"grader_identity": "<your-critic-id>", "items": '
        f'[{{"slug": "<one of {item_slugs}>", "status": "pass"|"fail", "citation": "<file:line or sketch field '
        f'naming WHY — an uncited pass records as a FAIL>"}}]}}`. One item per question.'
    )


def _sketch_block(ticket: "Ticket") -> str:
    directive = Directive.objects.linked_to(ticket)
    sketch = directive.sketch if directive is not None else None
    if sketch is None:
        return "  <no ratified sketch recorded — judge each item on its own merit>"
    rejected = "; ".join(sketch.rejected_alternatives) or "<none named>"
    return (
        f"  kind: {sketch.kind}\n"
        f"  setting: {sketch.setting_key} = {sketch.neutral_default!r} (neutral default)\n"
        f"  core chokepoint: {sketch.policy_chokepoint}\n"
        f"  activation: scope={sketch.activation_scope!r} value={sketch.activation_value!r}\n"
        f"  rejected alternatives (N=2 litmus): {rejected}"
    )


def covering_verdict(ticket: "Ticket", head_sha: str) -> "CriticVerdict | None":
    """The freshest recorded design verdict pinned to the exact plan *head_sha*."""
    return CriticVerdict.objects.latest_for(ticket=ticket, transition=_PLAN_TRANSITION, head_sha=head_sha)


def failed_slugs(verdict: "CriticVerdict") -> list[str]:
    """Plan rubric slugs the *verdict* FAILs (an uncited pass is downgraded to a fail)."""
    passed = {item.slug for item in verdict.item_verdicts() if not item.is_fail()}
    return [item.slug for item in llm_items(_PLAN_TRANSITION) if item.slug not in passed]


def _finding_specs(verdict: "CriticVerdict", head_sha: str) -> list[CriticFindingSpec]:
    specs: list[CriticFindingSpec] = []
    for failed in verdict.failed_items():
        item = item_for(failed.slug, _PLAN_TRANSITION)
        if item is None:
            continue  # a verdict slug outside the plan rubric is ignored, never a phantom finding
        status = (
            CriticFinding.Status.INSTRUMENTATION_GAP
            if failed.status == failed.INSTRUMENTATION_GAP
            else CriticFinding.Status.FAIL
        )
        specs.append(
            CriticFindingSpec(
                rubric_item=item.slug,
                detail=failed.citation or "design critic flagged this item without a citation",
                status=status,
                adversarial_question=item.adversarial_question,
                head_sha=head_sha,
            )
        )
    return specs


def record_design_findings(verdict: "CriticVerdict", *, ticket: "Ticket", head_sha: str) -> None:
    """Upsert a ``CriticFinding(transition="plan")`` per FAIL item; drop a now-clean item's stale row.

    The row set becomes the latest verdict — a plan item with no FAIL this pass has any
    prior finding removed, so a re-judged-clean plan clears its findings.
    """
    specs = _finding_specs(verdict, head_sha)
    flagged = {spec.rubric_item for spec in specs}
    CriticFinding.objects.filter(ticket=ticket, transition=_PLAN_TRANSITION).exclude(rubric_item__in=flagged).delete()
    for spec in specs:
        CriticFinding.record(ticket=ticket, transition=_PLAN_TRANSITION, spec=spec)


def _arm_design_critic(ticket: "Ticket", head_sha: str) -> None:
    """Arm the async design critic when no verdict covers the plan head (best-effort, idempotent)."""
    if covering_verdict(ticket, head_sha) is not None:
        return
    try:
        CriticDispatch.enqueue(
            ticket=ticket,
            transition=_PLAN_TRANSITION,
            head_sha=head_sha,
            contract=build_design_contract(ticket, head_sha),
        )
    except Exception as exc:  # noqa: BLE001 — self-healing enqueue; a failure re-arms next plan, never crashes the FSM.
        logger.warning("design critic dispatch enqueue failed for ticket %s: %s", ticket.pk, exc)


def _run_design_critic(ticket: "Ticket") -> None:
    directive = Directive.objects.linked_to(ticket)
    if directive is None:
        return  # ordinary ticket — the design critic is directive-only
    sketch: MechanismSketch | None = directive.sketch
    if sketch is None:
        return  # not yet interpreted — nothing ratified to judge
    head = plan_head_sha(ticket)
    if not head:
        return  # no base-bound plan yet — nothing to pin a verdict to
    verdict = covering_verdict(ticket, head)
    if verdict is None:
        _arm_design_critic(ticket, head)
        return
    record_design_findings(verdict, ticket=ticket, head_sha=head)


def check_design_critic(ticket: "Ticket") -> None:
    """Run the ADVISORY design critic at plan time — arm the async judge, record its findings, never block.

    A strict no-op while ``directive_loop_enabled`` is dark (one settings read, no DB
    query), so ordinary planning is untouched. When armed and the ticket implements a
    directive: arm the headless critic if no verdict covers the plan head, else mirror its
    FAIL items into ``CriticFinding(transition="plan")``. NEVER raises — the deterministic
    ``mechanism_conforms`` gate holds the blocking teeth; this only surfaces findings.
    """
    if not design_critic_armed(ticket.overlay or None):
        return
    try:
        _run_design_critic(ticket)
    except Exception as exc:  # noqa: BLE001 — advisory: a design-critic hiccup must never wedge the plan transition.
        logger.warning("design critic advisory pass failed for ticket %s: %s", ticket.pk, exc)


register_gate("design_critic", check_design_critic)
