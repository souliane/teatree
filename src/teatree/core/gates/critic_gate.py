"""critic_gate: the autonomous user-proxy critic on ``mark_delivered`` (SELFCATCH-5).

The unifying runtime of the self-catching layer — the single chokepoint where, at
the FSM's FINAL done-claim (RETRO_RECORDED→DELIVERED), the critic re-asks the
adversarial questions the human had to ask all session. Two halves:

Deterministic blocking teeth (no LLM in the blocking path)
    ``done_not_done`` / ``spec_not_plan`` / ``completeness`` are pure predicates over
    REAL artifacts (they REUSE ``merge_evidence_gate`` / ``plan_currency`` /
    ``rubric_gate``). A FAIL is recorded as a ``CriticFinding`` and raises
    :class:`CriticGateError` so delivery is refused. These are the only items that block.

Async LLM semantic net (advisory)
    ``coherence`` / ``duplication`` / ``deferred`` / ``ignored_input`` /
    ``unenforced_guarantee`` cannot be judged by determinism. When no fresh
    :class:`~teatree.core.models.critic_verdict.CriticVerdict` covers the delivered head,
    the gate ENQUEUES a headless critic on its OWN phase
    (:class:`~teatree.core.models.critic_dispatch.CriticDispatch`,
    ``phase="critic_reviewing"``) that reads the delivered artifacts and RETURNS a
    ``critic_verdict`` envelope; ``attempt_recorder`` records it server-side
    (maker≠checker). The gate mirrors the verdict's FAIL items into ``CriticFinding`` —
    advisory, never blocking.

Rollback safety
    ``mark_delivered`` runs inside ``transaction.atomic()``; a blocking raise would
    roll back the ``CriticFinding`` rows the gate just wrote. :class:`CriticGateError`
    therefore CARRIES the computed specs so the caller (``execute_retrospect``)
    re-records them OUTSIDE the rolled-back atomic — the operator sees the very
    findings the block tells them to fix.
"""

import logging
from pathlib import Path
from typing import TYPE_CHECKING

from teatree.core.gates.plan_currency_gate import latest_plan_artifact
from teatree.core.modelkit.gate_registry import register_gate
from teatree.core.models.attachment_manifest import AttachmentManifest
from teatree.core.models.critic_dispatch import CriticDispatch
from teatree.core.models.critic_finding import CriticFinding, CriticFindingSpec
from teatree.core.models.critic_verdict import (
    CRITIC_EVIDENCE_UNAVAILABLE_PREFIX,
    CriticEvidenceUnavailableError,
    CriticItemVerdict,
    CriticVerdict,
    CriticVerdictError,
)
from teatree.core.models.errors import CriticGateError
from teatree.core.models.merge_clear import MergeAudit
from teatree.core.models.ticket_worktree_checks import dispatch_worktree_path
from teatree.core.review.critic_rubric import deterministic_items, item_for, llm_items
from teatree.core.review.finding_standard import FINDING_STANDARD
from teatree.core.review.nit_lane import NitLaneError, validate_nit_comments
from teatree.core.review.nit_publish import critic_nits, pr_url_for_nits, publish_nits
from teatree.quality.review_evidence import ReviewEvidenceError, read_review_evidence

if TYPE_CHECKING:
    from teatree.core.models.ticket import Ticket

logger = logging.getLogger(__name__)

_TRANSITION = "mark_delivered"


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
    delivery atomic on success, or re-recorded outside it after a block).
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
        f"self-declared claim. This phase has no shell: use github_pr_diff, read_file, or search_files to inspect "
        f"the change. If the diff cannot be read, return unverified with verified=false for each ungraded item.\n\n"
        f"Finding standard: {FINDING_STANDARD}\n\n"
        f"Plan:\n{plan_text}\n\nUser-provided inputs (must be addressed or explicitly "
        f"declined):\n{inputs_block}\n\nQuestions:\n{questions}\n\nRETURN your judgment in the result envelope "
        f'(the phase has no shell — the orchestrator records it): `"critic_verdict": {{"grader_identity": '
        f'"<your-critic-id>", "items": [{{"slug": "<one of {item_slugs}>", "status": "pass"|"fail"|"unverified", '
        f'"verified": true|false, "citation": '
        f'"<file:line or artifact naming WHY — an uncited pass is recorded as a FAIL>"}}]}}`. One item per '
        f"question. A concern is `fail`; a clean item is `pass` WITH a citation of the artifact you inspected."
    )


def enqueue_llm_critic(ticket: "Ticket") -> None:
    """Arm the async critic when no fresh verdict covers the delivered head (best-effort)."""
    head_sha = delivered_head_sha(ticket)
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


def _publish_critic_nits(ticket: "Ticket", verdict: CriticVerdict, transition: str, head_sha: str) -> None:
    points = critic_nits(verdict.item_verdicts()) if transition == "merge" else ()
    if not points:
        return
    pr_url = pr_url_for_nits(ticket, head_sha)
    if not pr_url:
        message = "merge critic NIT has no attributable pull request URL"
        raise NitLaneError(message)
    publish_nits(ticket, pr_url, head_sha, points)


def _publish_recorded_critic_nits(dispatch: CriticDispatch, verdict: CriticVerdict) -> None:
    points = critic_nits(verdict.item_verdicts()) if dispatch.transition == "merge" else ()
    if points:
        dispatch.ticket.merge_extra(merge_into_dicts={"nit_publication": {dispatch.head_sha: {"status": "pending"}}})
    try:
        _publish_critic_nits(dispatch.ticket, verdict, dispatch.transition, dispatch.head_sha)
    except NitLaneError as exc:
        if points:
            dispatch.ticket.merge_extra(
                merge_into_dicts={"nit_publication": {dispatch.head_sha: {"status": "failed", "error": str(exc)}}}
            )
        raise
    if points:
        dispatch.ticket.merge_extra(merge_into_dicts={"nit_publication": {dispatch.head_sha: {"status": "published"}}})


def _evidence_row_refusal(ticket: "Ticket", head_sha: str, items: list[CriticItemVerdict]) -> str:
    worktree = dispatch_worktree_path(ticket)
    if not worktree:
        return ""
    evidence = read_review_evidence(Path(worktree), head_sha)
    if evidence is None:
        return ""
    points = {item.slug: item.points for item in items}
    for row in evidence.matrix.rows:
        if row.redundant_with and not any(
            point.kind == "blocking" and row.node in f"{point.citation} {point.summary}"
            for point in points.get("test_value", ())
        ):
            return f"redundant_with row {row.node} needs a blocking test_value point citing that node"
    for duplicate in evidence.duplicates:
        location = f"{duplicate.path}:{duplicate.second_line}"
        if not any(
            point.kind == "blocking" and location in f"{point.citation} {point.file}:{point.line} {point.summary}"
            for point in points.get("cleanliness", ())
        ):
            return f"duplicate definition {location} needs a blocking cleanliness point citing that location"
    return ""


def _record_new_critic_verdict(dispatch: CriticDispatch, envelope: dict) -> CriticVerdict:
    raw_items = envelope.get("items")
    if dispatch.transition == "merge" and isinstance(raw_items, list):
        nit_follow_up = isinstance(dispatch.ticket.extra, dict) and dispatch.ticket.extra.get("nit_follow_up") is True
        items = [
            CriticItemVerdict.coerce(item, nit_follow_up=nit_follow_up) for item in raw_items if isinstance(item, dict)
        ]
        validate_nit_comments(critic_nits(items))
        if refusal := _evidence_row_refusal(dispatch.ticket, dispatch.head_sha, items):
            raise CriticEvidenceUnavailableError(refusal)
    return CriticVerdict.record_from_envelope(
        ticket=dispatch.ticket,
        transition=dispatch.transition,
        head_sha=dispatch.head_sha,
        envelope=envelope,
    )


def record_returned_critic_verdict(task: object, result: dict) -> str:
    """Record a headless critic task's returned ``critic_verdict`` envelope (corr-11).

    The orchestrator half of the async critic lane, mirroring
    ``review_envelope_recorder.record_returned_review_envelope``: a Bash-denied critic RETURNS a
    typed ``critic_verdict``; THIS actor (not the maker) records the
    :class:`CriticVerdict`. Only a ``mark_delivered`` verdict then re-runs the delivery
    rubric so its LLM items land in ``CriticFinding``; a merge or plan verdict is mirrored
    by its own gate, and re-running here would judge ``done_not_done`` before the merge.
    A non-critic task, a result without a ``critic_verdict``, or an unresolvable dispatch
    is a no-op (``""``). Returns an error string when the verdict is maker-graded so the
    caller fails the task and the block surfaces.
    """
    dispatch = getattr(task, "critic_dispatches", None)
    dispatch_row = dispatch.first() if dispatch is not None else None
    raw_envelope = result.get("critic_verdict")
    if dispatch_row is None or not isinstance(raw_envelope, dict):
        return ""
    summary = str(result.get("summary") or "").casefold()
    if any(
        phrase in summary
        for phrase in (
            "could not read the diff",
            "cannot read the diff",
            "unable to read the diff",
            "could not grade the change",
        )
    ):
        return f"{CRITIC_EVIDENCE_UNAVAILABLE_PREFIX} critic reported that it could not inspect the change"
    if dispatch_row.transition == "merge":
        expected = {item.slug for item in llm_items("merge")}
        raw_items = raw_envelope.get("items")
        received = (
            [str(item.get("slug") or "") for item in raw_items if isinstance(item, dict)]
            if isinstance(raw_items, list)
            else []
        )
        if len(received) != len(expected) or set(received) != expected:
            return (
                f"{CRITIC_EVIDENCE_UNAVAILABLE_PREFIX} merge critic must grade each item exactly once: "
                f"{', '.join(sorted(expected))}"
            )
    ticket = dispatch_row.ticket
    try:
        verdict = CriticVerdict.objects.latest_for(
            ticket=ticket, transition=dispatch_row.transition, head_sha=dispatch_row.head_sha
        )
        if verdict is None:
            verdict = _record_new_critic_verdict(dispatch_row, raw_envelope)
        _publish_recorded_critic_nits(dispatch_row, verdict)
    except (CriticEvidenceUnavailableError, ReviewEvidenceError) as exc:
        return f"{CRITIC_EVIDENCE_UNAVAILABLE_PREFIX} {exc}"
    except (CriticVerdictError, NitLaneError) as exc:
        return f"critic verdict recording refused: {exc}"
    if dispatch_row.transition == _TRANSITION:
        record_critic_findings(ticket, run_critic(ticket))
    return ""


def blocking_specs(specs: list[CriticFindingSpec]) -> list[CriticFindingSpec]:
    """The subset of *specs* whose rubric item BLOCKS under enforcement (the deterministic teeth)."""
    return [spec for spec in specs if (item := item_for(spec.rubric_item, _TRANSITION)) is not None and item.blocking]


def check_critic(ticket: "Ticket") -> None:
    """Record findings, dispatch the LLM critic, and refuse blocking deterministic findings."""
    specs = run_critic(ticket)
    record_critic_findings(ticket, specs)
    enqueue_llm_critic(ticket)
    blockers = blocking_specs(specs)
    if not blockers:
        return
    items = ", ".join(f"{spec.rubric_item} ({spec.status})" for spec in blockers)
    msg = (
        f"Refusing to mark ticket {ticket.pk} DELIVERED — the critic found {len(blockers)} unresolved "
        f"blocking issue(s): {items}. Each is recorded as a CriticFinding naming the offending artifact — "
        f"resolve the findings and re-run delivery."
    )
    raise CriticGateError(msg, specs=specs)


register_gate("critic", check_critic)
