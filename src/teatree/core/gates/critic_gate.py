"""critic_gate: the autonomous user-proxy critic on ``mark_delivered`` (SELFCATCH-5).

The unifying runtime of the self-catching layer — the single chokepoint where, at
the FSM's FINAL done-claim (RETROSPECTED→DELIVERED), the critic re-asks the
adversarial questions the human had to ask all session. Two halves:

Deterministic blocking teeth (no LLM in the blocking path)
    ``done_not_done`` / ``spec_not_plan`` / ``completeness`` are pure predicates over
    REAL artifacts (they REUSE ``merge_evidence_gate`` / ``plan_currency`` /
    ``spec_coverage_gate``). A FAIL is recorded as a ``CriticFinding`` and, when
    ``critic_gate_live`` is on, raises :class:`CriticGateError` so the delivery is
    refused. These are the ONLY items that can block.

Async LLM semantic net (advisory)
    ``coherence`` / ``duplication`` / ``deferred`` / ``ignored_input`` /
    ``unenforced_guarantee`` cannot be judged by determinism. When ``critic_gate_live``
    is on and no fresh :class:`~teatree.core.models.critic_verdict.CriticVerdict`
    covers the delivered head, the gate ENQUEUES a headless critic on its OWN phase
    (:class:`~teatree.core.models.critic_dispatch.CriticDispatch`,
    ``phase="critic_reviewing"``) that reads the delivered artifacts and RETURNS a
    ``critic_verdict`` envelope; ``attempt_recorder`` records it server-side
    (maker≠checker). The gate mirrors the verdict's FAIL items into ``CriticFinding`` —
    advisory, never blocking.

Advisory-first, cost-safe while dark
    ``critic_gate_live`` (DARK, default OFF) gates BOTH the BLOCKING raise AND the
    EXPENSIVE async LLM dispatch. The cheap deterministic findings are recorded on every
    delivery (advisory evidence); the async ``claude -p`` critic is armed only once an
    overlay opts in — so a customer overlay that never sets the flag creates no
    Session/Task/CriticDispatch. Enablement is per-overlay, the teatree/dogfood overlay
    first. Its own kill-switch (set it back OFF) is the never-lockout escape.

Enforcing-mode rollback safety
    ``mark_delivered`` runs inside ``transaction.atomic()``; a blocking raise would
    roll back the ``CriticFinding`` rows the gate just wrote. :class:`CriticGateError`
    therefore CARRIES the computed specs so the caller (``execute_retrospect``)
    re-records them OUTSIDE the rolled-back atomic — the operator sees the very
    findings the block tells them to fix.
"""

import logging
from typing import TYPE_CHECKING

from teatree.config import get_effective_settings
from teatree.core.gates.plan_currency_gate import latest_plan_artifact
from teatree.core.modelkit.gate_registry import register_gate
from teatree.core.models.attachment_manifest import AttachmentManifest
from teatree.core.models.critic_dispatch import CriticDispatch
from teatree.core.models.critic_finding import CriticFinding, CriticFindingSpec
from teatree.core.models.critic_verdict import CriticVerdict, CriticVerdictError
from teatree.core.models.errors import CriticGateError
from teatree.core.models.merge_clear import MergeAudit
from teatree.core.review.critic_rubric import deterministic_items, item_for, llm_items

if TYPE_CHECKING:
    from teatree.core.models.ticket import Ticket

logger = logging.getLogger(__name__)

_TRANSITION = "mark_delivered"


def critic_enforcement_live(overlay_name: str | None) -> bool:
    """Whether a BLOCKING critic finding blocks delivery for *overlay_name* (overlay -> global)."""
    return bool(get_effective_settings(overlay_name).critic_gate_live)


def delivered_head_sha(ticket: "Ticket") -> str:
    """The delivered tree's SHA — the keystone MergeAudit merged_sha, or '' when none."""
    for sha in MergeAudit.objects.filter(clear__ticket=ticket).values_list("merged_sha", flat=True):
        if sha and sha.strip():
            return sha.strip().lower()
    return ""


def _deterministic_specs(ticket: "Ticket", head_sha: str) -> list[CriticFindingSpec]:
    specs: list[CriticFindingSpec] = []
    for item in deterministic_items(_TRANSITION):
        try:
            detail = item.evaluate(ticket)
        except Exception as exc:  # noqa: BLE001 — an inconclusive predicate is a finding, not a crash of the gate.
            logger.warning("critic deterministic item %r raised over ticket %s: %s", item.slug, ticket.pk, exc)
            specs.append(
                CriticFindingSpec(
                    rubric_item=item.slug,
                    detail=f"predicate raised (inconclusive): {exc}",
                    status=CriticFinding.Status.INSTRUMENTATION_GAP,
                    adversarial_question=item.adversarial_question,
                    head_sha=head_sha,
                )
            )
            continue
        if detail:
            specs.append(
                CriticFindingSpec(
                    rubric_item=item.slug,
                    detail=detail,
                    adversarial_question=item.adversarial_question,
                    head_sha=head_sha,
                )
            )
    return specs


def _llm_specs(ticket: "Ticket", head_sha: str) -> list[CriticFindingSpec]:
    """Mirror the freshest recorded LLM verdict's FAIL items into finding specs.

    Reads the newest :class:`CriticVerdict` for the delivery (a prior async critic
    run wrote it). No verdict yet → no LLM specs this pass (the gate enqueues one);
    the findings appear once the async critic completes and re-runs.
    """
    verdict = CriticVerdict.objects.latest_for(ticket=ticket, transition=_TRANSITION, head_sha=head_sha)
    if verdict is None:
        return []
    specs: list[CriticFindingSpec] = []
    for failed in verdict.failed_items():
        item = item_for(failed.slug, _TRANSITION)
        if item is None:
            continue  # a verdict slug outside the rubric is ignored, never recorded as a phantom finding
        status = (
            CriticFinding.Status.INSTRUMENTATION_GAP
            if failed.status == failed.INSTRUMENTATION_GAP
            else CriticFinding.Status.FAIL
        )
        detail = failed.citation or "LLM critic flagged this item without a citation"
        specs.append(
            CriticFindingSpec(
                rubric_item=item.slug,
                detail=detail,
                status=status,
                adversarial_question=item.adversarial_question,
                head_sha=head_sha,
            )
        )
    return specs


def run_critic(ticket: "Ticket") -> list[CriticFindingSpec]:
    """Compute every finding over *ticket* — PURE, no DB writes.

    Deterministic items are evaluated live; LLM items are mirrored from the freshest
    recorded verdict. Returned so the caller decides how to persist them (inside the
    delivery atomic for the advisory path, or re-recorded outside it after a block).
    """
    head_sha = delivered_head_sha(ticket)
    return _deterministic_specs(ticket, head_sha) + _llm_specs(ticket, head_sha)


def record_critic_findings(ticket: "Ticket", specs: list[CriticFindingSpec]) -> None:
    """Upsert a CriticFinding per spec; delete a now-clean item's stale finding.

    The row set becomes the LATEST verdict (not an append-only log): a rubric item
    with no spec this pass has any prior finding removed.
    """
    flagged = {spec.rubric_item for spec in specs}
    CriticFinding.objects.filter(ticket=ticket, transition=_TRANSITION).exclude(rubric_item__in=flagged).delete()
    for spec in specs:
        CriticFinding.record(ticket=ticket, transition=_TRANSITION, spec=spec)


def build_critic_contract(ticket: "Ticket", head_sha: str) -> str:
    """The dispatch contract for the headless critic — injects the ACTIVE LLM rubric + real artifacts.

    Rubric evolution changes the critic's behaviour with zero prompt edits: the LLM
    items are read from the registry, and the real delivered artifacts (plan text,
    intake attachment URLs) are named so the model judges against them, not against a
    self-declared claim. The contract instructs the critic to RETURN a
    ``critic_verdict`` envelope (corr-11) — it has no shell to record it itself.
    """
    questions = "\n".join(f"  - {item.slug}: {item.adversarial_question}" for item in llm_items(_TRANSITION))
    plan = latest_plan_artifact(ticket)
    plan_text = (plan.plan_text if plan else "").strip() or "<no plan recorded>"
    manifest = AttachmentManifest.latest_for(ticket)
    entries = manifest.entries if manifest else []
    inputs = [str(e.get("source_url") or "").strip() for e in entries if isinstance(e, dict)]
    inputs_block = "\n".join(f"  - {url}" for url in inputs if url) or "  - <none recorded>"
    item_slugs = ", ".join(item.slug for item in llm_items(_TRANSITION))
    return (
        f"You are the autonomous user-proxy CRITIC judging the DELIVERY of ticket {ticket.pk}. Read the "
        f"delivered artifacts — the merged PR diff at head {head_sha[:8] or '<unknown>'}, the plan below, and the "
        f"intake attachments below — and answer each semantic question. Judge against the ARTIFACTS, never a "
        f"self-declared claim.\n\nPlan:\n{plan_text}\n\nUser-provided inputs (must be addressed or explicitly "
        f"declined):\n{inputs_block}\n\nQuestions:\n{questions}\n\nRETURN your judgment in the result envelope "
        f'(the phase has no shell — the orchestrator records it): `"critic_verdict": {{"grader_identity": '
        f'"<your-critic-id>", "items": [{{"slug": "<one of {item_slugs}>", "status": "pass"|"fail", "citation": '
        f'"<file:line or artifact naming WHY — an uncited pass is recorded as a FAIL>"}}]}}`. One item per '
        f"question. A concern is `fail`; a clean item is `pass` WITH a citation of the artifact you inspected."
    )


def _enqueue_llm_critic(ticket: "Ticket", head_sha: str) -> None:
    """Arm the async critic when no fresh verdict covers the delivered head (best-effort)."""
    if CriticVerdict.objects.latest_for(ticket=ticket, transition=_TRANSITION, head_sha=head_sha) is not None:
        return
    try:
        CriticDispatch.enqueue(
            ticket=ticket,
            transition=_TRANSITION,
            head_sha=head_sha,
            contract=build_critic_contract(ticket, head_sha),
        )
    except Exception as exc:  # noqa: BLE001 — the enqueue is self-healing; a failure re-arms next tick, never crashes delivery.
        logger.warning("critic dispatch enqueue failed for ticket %s: %s", ticket.pk, exc)


def record_returned_critic_verdict(task: object, result: dict) -> str:
    """Record a headless critic task's returned ``critic_verdict`` envelope (corr-11).

    The orchestrator half of the async critic lane, mirroring
    ``attempt_recorder._maybe_record_review_verdict``: a Bash-denied critic RETURNS a
    typed ``critic_verdict``; THIS actor (not the maker) records the
    :class:`CriticVerdict`, then re-runs the finding recording so the freshly-judged
    LLM items land in ``CriticFinding``. A non-critic task, a result without a
    ``critic_verdict``, or an unresolvable dispatch is a no-op (``""``). Returns an
    error string when the verdict is maker-graded so the caller fails the task and the
    block surfaces.
    """
    dispatch = getattr(task, "critic_dispatches", None)
    dispatch_row = dispatch.first() if dispatch is not None else None
    if dispatch_row is None:
        return ""
    raw_envelope = result.get("critic_verdict")
    if not isinstance(raw_envelope, dict):
        return ""
    ticket = dispatch_row.ticket
    try:
        CriticVerdict.record_from_envelope(
            ticket=ticket,
            transition=dispatch_row.transition,
            head_sha=dispatch_row.head_sha,
            envelope=raw_envelope,
        )
    except CriticVerdictError as exc:
        return f"critic verdict recording refused: {exc}"
    record_critic_findings(ticket, run_critic(ticket))
    return ""


def blocking_specs(specs: list[CriticFindingSpec]) -> list[CriticFindingSpec]:
    """The subset of *specs* whose rubric item BLOCKS under enforcement (the deterministic teeth)."""
    return [spec for spec in specs if (item := item_for(spec.rubric_item, _TRANSITION)) is not None and item.blocking]


def check_critic(ticket: "Ticket") -> None:
    """Run the critic at ``mark_delivered``; block only on a deterministic BLOCKING finding when live.

    Always records the cheap deterministic findings (advisory). Only when
    ``critic_gate_live`` is on for the ticket's overlay does it (a) arm the EXPENSIVE
    async LLM critic and (b) raise :class:`CriticGateError` on a BLOCKING deterministic
    item — carrying the specs so the caller re-records them outside the rolled-back
    delivery atomic. DARK (default) ⇒ no LLM dispatch, no block: truly inert on the
    expensive path.
    """
    specs = run_critic(ticket)
    record_critic_findings(ticket, specs)
    if not critic_enforcement_live(ticket.overlay or None):
        # DARK (default): the deterministic findings above are cheap advisory
        # evidence, but the async LLM critic is EXPENSIVE (a headless `claude -p`
        # reading plan+diff+attachments). Ship it truly inert — no Session/Task/
        # CriticDispatch created — until an overlay opts in via `critic_gate_live`
        # (the per-overlay flag scopes enablement to the teatree/dogfood overlay
        # first, exactly like `require_merge_evidence`/`require_plan_adequacy`).
        return
    _enqueue_llm_critic(ticket, delivered_head_sha(ticket))
    blockers = blocking_specs(specs)
    if not blockers:
        return
    items = ", ".join(f"{spec.rubric_item} ({spec.status})" for spec in blockers)
    msg = (
        f"Refusing to mark ticket {ticket.pk} DELIVERED — the critic found {len(blockers)} unresolved "
        f"blocking issue(s): {items}. Each is recorded as a CriticFinding naming the offending artifact — "
        f"resolve them, then re-run delivery. If a finding is a genuine false positive the operator's audited "
        f"escape is to disable enforcement: `t3 <overlay> config_setting set critic_gate_live false --overlay "
        f"<name>` (advisory recording continues)."
    )
    raise CriticGateError(msg, specs=specs)


register_gate("critic", check_critic)
