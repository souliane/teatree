"""The merge-quality critic gate (north-star PR-4) — clean+tested-enough as a MERGE gate.

The design's second differentiating tooth: a merely-GREEN auto-change that is not
well-engineered must not merge. Two LLM rubric items at ``transition="merge"`` —
``test_value`` (anti-vacuity AND anti-bloat, anchored to the ratified
``test_strategy``) and ``cleanliness`` (the CLAUDE.md bar) — are judged by the
headless critic on its own ``critic_reviewing`` phase, exactly like the
``mark_delivered`` semantic set. This module is the DETERMINISTIC gate over their
recorded verdict, split judging from gating in time so no LLM runs in the blocking
path (the critic-gate doctrine, preserved):

The judge (async)
    records a :class:`~teatree.core.models.critic_verdict.CriticVerdict` keyed
    ``(ticket, "merge", head_sha)`` — armed here when no covering verdict yet
    exists, so the gate is SATISFIABLE (the async critic records the verdict, the
    next merge attempt passes), never pure suppression.

The gate (deterministic)
    a pure row-check at merge time: a verdict covering the EXACT shipped head must
    exist AND affirmatively PASS every merge rubric item (a FAIL, an uncited pass
    downgraded to ``instrumentation_gap``, or an OMITTED item all block — the
    anti-vacuity floor). Fail-closed, exactly like "CI must be green".

Every ticket requires a verdict at the shipped head, including ordinary and
directive tickets.

Wired at :func:`~teatree.core.merge.execution.execute_bound_merge` — the single
chokepoint BOTH autonomous merge paths cross, mirroring ``assert_review_verdict_gate``.
"""

import logging
from pathlib import Path
from typing import TYPE_CHECKING, cast

from teatree.core.gates.plan_currency_gate import latest_plan_artifact
from teatree.core.merge.errors import MergePreconditionError
from teatree.core.merge.ticket_resolution import resolve_gated_ticket
from teatree.core.models.critic_dispatch import CriticDispatch
from teatree.core.models.critic_finding import CriticFinding, CriticFindingSpec
from teatree.core.models.critic_verdict import CriticVerdict
from teatree.core.models.directive import Directive
from teatree.core.models.review_verdict import ReviewVerdict
from teatree.core.models.task import Task
from teatree.core.models.ticket_worktree_checks import dispatch_worktree_path
from teatree.core.review.critic_continuity import (
    merge_critic_continuity,
    merge_verdict_history,
    review_evidence_contract,
)
from teatree.core.review.critic_rubric import (
    _MERGE_TRANSITION,
    EXHAUSTIVE_TEST_VALUE_RULE,
    item_for,
    llm_items,
    merge_item_questions,
)
from teatree.core.review.finding_standard import FINDING_STANDARD
from teatree.core.review.nit_publish import critic_nits, has_nit_receipts, pr_url_for_nits, publish_nits, review_nits
from teatree.core.worktree.target_branch import resolve_target_branch
from teatree.quality.brief_constraints import extract_constraints
from teatree.quality.changed_set import ChangedSetError
from teatree.quality.push_gate import run_brief_constraints
from teatree.utils.run import CommandFailedError
from teatree.utils.singleton import WORKER_SINGLETON, flock_is_held

if TYPE_CHECKING:
    from collections.abc import Mapping

    from teatree.core.models.ticket import Ticket

logger = logging.getLogger(__name__)


class MergeQualityVerdictError(MergePreconditionError):
    """No clean merge-quality ``CriticVerdict`` covers the shipped head — refuse the merge.

    A :class:`~teatree.core.merge.errors.MergePreconditionError` subclass so the
    keystone's single re-escalation path surfaces it and the loop leaves the FSM
    untouched, exactly like every other merge precondition.
    """


def linked_directive(ticket: "Ticket") -> "Directive | None":
    """The :class:`Directive` this ticket implements, or ``None`` for ordinary work.

    The single resolver — ``extra["directive_id"]`` marker first, then the reverse FK —
    lives on the manager (``Directive.objects.linked_to``) so the plan-time and merge-time
    directive gates identify a directive ticket by the identical rule.
    """
    return Directive.objects.linked_to(ticket)


def is_directive_ticket(ticket: "Ticket") -> bool:
    return linked_directive(ticket) is not None


def ratified_test_strategy(ticket: "Ticket") -> str:
    """The ratified ``test_strategy`` the ``test_value`` item is judged against.

    A directive ticket's anchor is its ratified :class:`MechanismSketch`'s named
    ``acceptance_tests``; an ordinary ticket's is its plan's ``test_strategy``
    adequacy section (falling back to the plan text). The critic judges bloat
    (tests beyond the strategy) and vacuity (strategy not delivered) against it.
    """
    directive = linked_directive(ticket)
    if directive is not None:
        sketch = directive.sketch
        if sketch is not None and sketch.acceptance_tests:
            named = "\n".join(f"  - {node_id}" for node_id in sketch.acceptance_tests)
            return f"the ratified sketch's acceptance tests (the mechanism must add exactly these):\n{named}"
    plan = latest_plan_artifact(ticket)
    if plan is not None:
        section_text = _test_strategy_section(plan.adequacy)
        if section_text:
            return f"the plan's ratified test_strategy section:\n{section_text}"
        if plan.plan_text.strip():
            return f"the plan (no discrete test_strategy section recorded):\n{plan.plan_text.strip()}"
    return "<no ratified test_strategy recorded — judge each test on its own merit>"


def _test_strategy_section(adequacy: object) -> str:
    """The ``test_strategy`` adequacy section's content as text, or ``""``."""
    if not isinstance(adequacy, dict):
        return ""
    section = cast("Mapping[str, object]", adequacy).get("test_strategy")
    if not isinstance(section, dict):
        return ""
    fields = cast("Mapping[str, object]", section)
    content = fields.get("content")
    if isinstance(content, list):
        return "\n".join(f"  - {str(item).strip()}" for item in content if str(item).strip())
    if isinstance(content, str) and content.strip():
        return content.strip()
    none_reason = fields.get("none_reason")
    if isinstance(none_reason, str) and none_reason.strip():
        return f"  (declared no tests: {none_reason.strip()})"
    return ""


def build_merge_quality_contract(ticket: "Ticket", head_sha: str) -> str:
    """The dispatch contract for the headless merge-quality critic (the merge rubric injected).

    Names the merge LLM items from the registry (a forgotten item would never be
    judged), anchors ``test_value`` to the ratified ``test_strategy``, and instructs
    the critic to RETURN a ``critic_verdict`` envelope — the same corr-11 shape the
    ``mark_delivered`` critic returns, judged at the shared ``critic_reviewing`` phase.
    """
    item_slugs = ", ".join(item.slug for item in llm_items(_MERGE_TRANSITION))
    strategy = ratified_test_strategy(ticket)
    context = "\n\n".join(
        part
        for part in (
            merge_critic_continuity(ticket, head_sha),
            merge_verdict_history(ticket),
            review_evidence_contract(ticket, head_sha),
        )
        if part
    )
    return (
        f"You are the merge-quality CRITIC deciding whether the shipped change on ticket {ticket.pk} at head "
        f"{head_sha[:8] or '<unknown>'} is not merely green but WELL-ENGINEERED — clean and tested-enough WITHOUT "
        f"bloat. This phase has no shell: use github_pr_diff for the PR diff and read_file or search_files for "
        f"touched modules. Answer each item against the ARTIFACTS. If the diff cannot be read, return "
        f"unverified with verified=false for each ungraded item; do not call it fail.\n\n"
        f"Finding standard: {FINDING_STANDARD} Every point carries kind: "
        f'"kind": "blocking"|"nit". A NIT alone cannot fail an item.\n\n'
        f"A redundant_with row needs a blocking test_value point citing its test node; a duplicate definition "
        f"needs a blocking cleanliness point citing its file:line. An ignored row is refused and re-armed.\n\n"
        f"{context}\n\nAnchor for `test_value` — {strategy}\n\n"
        f"On a later head, each item's resolutions array must name every prior finding ID under that item, "
        f"state fixed or not_fixed, and cite the current artifact. "
        f"A not_fixed BLOCKING point keeps its item failed.\n\n"
        f"`test_value` is BOTH directions: a VACUOUS test (asserts nothing that could fail — a tautology, the "
        f"framework, a mock talking to a mock) FAILS, AND a BLOATED test set (redundant permutations beyond the "
        f"ratified strategy, same behavior/same failure mode in different dressing) FAILS; the coverage-that-matters "
        f"from the strategy must be present with no undeclared redundant tests. {EXHAUSTIVE_TEST_VALUE_RULE} "
        f"`cleanliness` is the CLAUDE.md bar: "
        f"full typing (no smuggled Any), composition over inheritance, Django conventions, self-documenting names, "
        f"no parallel mechanism where a substrate convention exists, docs/BLUEPRINT left consistent.\n\n"
        f"Questions:\n{merge_item_questions()}\n\n"
        f"RETURN your judgment in the result envelope (the phase has no shell — the "
        f'orchestrator records it): `"critic_verdict": {{"grader_identity": "<your-critic-id>", "items": '
        f'[{{"slug": "<one of {item_slugs}>", "status": "pass"|"fail"|"unverified", "verified": true|false, '
        f'"citation": "<test node id or file:line naming WHY — an uncited pass records as a FAIL>", '
        f'"points": [{{"kind": "blocking"|"nit", "summary": "<specific finding>", '
        f'"citation": "<file:line>", "file": "<changed path>", "line": <line>}}], '
        f'"resolutions": [{{"finding_id": "<prior point id or failed item id>", '
        f'"status": "fixed"|"not_fixed", "citation": "<current file:line>"}}]}}]}}`. '
        f"One item per question — judge BOTH."
    )


def covering_verdict(ticket: "Ticket", head_sha: str) -> "CriticVerdict | None":
    """The freshest recorded merge verdict pinned to the exact shipped *head_sha*."""
    return CriticVerdict.objects.latest_for(ticket=ticket, transition=_MERGE_TRANSITION, head_sha=head_sha)


def unmet_items(verdict: "CriticVerdict") -> list[str]:
    """Merge rubric slugs the *verdict* does not affirmatively PASS (the anti-vacuity floor).

    A slug is met only by an OK item WITH a citation (``CriticItemVerdict.coerce``
    downgrades an uncited pass to ``instrumentation_gap``, which ``is_fail``).
    A FAIL, an uncited pass, OR an OMITTED item is unmet — a critic cannot wave a
    merge through by staying silent on an item.
    """
    passed = {item.slug for item in verdict.item_verdicts() if item.status == item.OK and item.verified}
    return [item.slug for item in llm_items(_MERGE_TRANSITION) if item.slug not in passed]


def _finding_specs(verdict: "CriticVerdict", head_sha: str) -> list[CriticFindingSpec]:
    specs: list[CriticFindingSpec] = []
    for failed in verdict.failed_items():
        item = item_for(failed.slug, _MERGE_TRANSITION)
        if item is None:
            continue  # a verdict slug outside the merge rubric is ignored, never a phantom finding
        status = (
            CriticFinding.Status.INSTRUMENTATION_GAP
            if failed.status == failed.INSTRUMENTATION_GAP
            else CriticFinding.Status.FAIL
        )
        specs.append(
            CriticFindingSpec(
                rubric_item=item.slug,
                detail=failed.citation or "merge-quality critic flagged this item without a citation",
                status=status,
                adversarial_question=item.adversarial_question,
                head_sha=head_sha,
            )
        )
    return specs


def record_merge_quality_findings(verdict: "CriticVerdict", *, ticket: "Ticket", head_sha: str) -> None:
    """Upsert a ``CriticFinding(transition="merge")`` per FAIL item; drop a now-clean item's stale row.

    The row set becomes the latest verdict — a merge item with no FAIL this pass
    has any prior finding removed, so a re-judged-clean head clears its findings.
    """
    specs = _finding_specs(verdict, head_sha)
    flagged = {spec.rubric_item for spec in specs}
    CriticFinding.objects.filter(ticket=ticket, transition=_MERGE_TRANSITION).exclude(rubric_item__in=flagged).delete()
    for spec in specs:
        CriticFinding.record(ticket=ticket, transition=_MERGE_TRANSITION, spec=spec)


def _arm_merge_quality_critic(ticket: "Ticket", head_sha: str) -> None:
    """Arm the async merge-quality critic when no verdict covers the head (best-effort, idempotent)."""
    if covering_verdict(ticket, head_sha) is not None:
        return
    try:
        CriticDispatch.enqueue(
            ticket=ticket,
            transition=_MERGE_TRANSITION,
            head_sha=head_sha,
            contract=build_merge_quality_contract(ticket, head_sha),
        )
    except Exception as exc:  # noqa: BLE001 — self-healing enqueue; a failure re-arms next attempt, never crashes the merge.
        logger.warning("merge-quality critic dispatch enqueue failed for ticket %s: %s", ticket.pk, exc)


def check_merge_quality_verdict(ticket: "Ticket", head_sha: str) -> None:
    """Refuse the merge unless a clean merge-quality verdict covers *head_sha*.

    For every ticket, fail closed: no covering verdict → arm the async critic and refuse; a verdict
    with any unmet merge item → record the FAIL findings and refuse; every item
    affirmatively passed → proceed.
    """
    head = head_sha.strip().lower()
    _check_brief_constraints(ticket)
    verdict = covering_verdict(ticket, head)
    if verdict is None:
        _arm_merge_quality_critic(ticket, head)
        from teatree.config.resolution import worker_is_quiescing  # noqa: PLC0415 — deferred: reads live worker config

        if CriticDispatch.saturated().filter(ticket=ticket, transition=_MERGE_TRANSITION, head_sha=head).exists():
            recovery = (
                "The critic retry budget is exhausted; inspect the failed critic tasks, repair the cause, "
                "and rearm the claim before retrying."
            )
        elif worker_is_quiescing():
            recovery = "The worker is quiescing; resume worker admission, then let the queued critic run and retry."
        elif not flock_is_held(WORKER_SINGLETON):
            recovery = "The worker is stopped; start the worker, let the queued critic run, then retry."
        else:
            recovery = "A headless critic has been armed; retry once it records a clean verdict."
        msg = (
            f"no recorded merge-quality CriticVerdict covers the shipped head {head} for ticket {ticket.pk} — "
            f"refusing to merge (north-star PR-4). A clean-and-tested-enough verdict (test_value + cleanliness) at "
            f"the exact head is required before a merge. {recovery}"
        )
        raise MergeQualityVerdictError(msg)
    record_merge_quality_findings(verdict, ticket=ticket, head_sha=head)
    unmet = unmet_items(verdict)
    if unmet:
        msg = (
            f"the merge-quality CriticVerdict at head {head} for ticket {ticket.pk} does not clear "
            f"{', '.join(unmet)} — refusing to merge (north-star PR-4): merely-green is not well-engineered. Each "
            f"unmet item is recorded as a CriticFinding naming the offending test/file — resolve them and re-judge "
            f"at the shipped head."
        )
        raise MergeQualityVerdictError(msg)
    _assert_nit_receipts(ticket, head, verdict)


def _check_brief_constraints(ticket: "Ticket") -> None:
    plan = latest_plan_artifact(ticket)
    task = Task.objects.filter(ticket=ticket, phase__in=("coding", "debugging")).order_by("-created_at", "-pk").first()
    brief = "\n".join(
        part
        for part in (
            plan.plan_text if plan is not None else "",
            task.execution_reason if task is not None else "",
        )
        if part
    )
    if not extract_constraints(brief):
        return
    worktree = dispatch_worktree_path(ticket)
    if not worktree:
        message = "brief constraints cannot be verified: ticket worktree is missing"
        raise MergeQualityVerdictError(message)
    try:
        violations = run_brief_constraints(
            Path(worktree), base_ref=resolve_target_branch(ticket, worktree), brief=brief
        )
    except (ChangedSetError, CommandFailedError, OSError) as exc:
        message = f"brief constraints could not be verified: {exc}"
        raise MergeQualityVerdictError(message) from exc
    if violations:
        message = f"brief constraint failed: {violations[0].detail}"
        raise MergeQualityVerdictError(message)


def _assert_nit_receipts(ticket: "Ticket", head_sha: str, verdict: "CriticVerdict") -> None:
    review_points = tuple(
        point
        for review in ReviewVerdict.objects.filter(ticket=ticket, reviewed_sha=head_sha)
        for point in review_nits(review.structured_findings)
    )
    points = tuple(dict.fromkeys((*critic_nits(verdict.item_verdicts()), *review_points)))
    if not points:
        return
    pr_url = pr_url_for_nits(ticket, head_sha)
    publication = ticket.extra.get("nit_publication", {}) if isinstance(ticket.extra, dict) else {}
    marker = publication.get(head_sha, {}) if isinstance(publication, dict) else {}
    if pr_url and marker.get("status") in {"pending", "failed"}:
        try:
            publish_nits(ticket, pr_url, head_sha, points)
        except RuntimeError as exc:
            message = f"NIT publication retry refused: {exc}"
            raise MergeQualityVerdictError(message) from exc
        ticket.refresh_from_db()
        ticket.merge_extra(merge_into_dicts={"nit_publication": {head_sha: {"status": "published"}}})
    if not pr_url or not has_nit_receipts(ticket, pr_url, head_sha, points):
        message = (
            f"NIT publication receipt is missing for {ticket.pk} at head {head_sha}; "
            "publish each inline point, read back the follow-up issue and resolved threads, then retry"
        )
        raise MergeQualityVerdictError(message)


def assert_merge_quality_verdict(*, slug: str, pr_id: int, head_sha: str) -> None:
    """The ``execute_bound_merge`` chokepoint entry — resolve the ticket then run the gate.

    Resolves the gated ticket from the PR ``(repo, iid)`` ledger (case-insensitive)
    OR the ``MergeClear`` for the same ``(slug, pr_id)``
    (:func:`~teatree.core.merge.ticket_resolution.resolve_gated_ticket`),
    so a missing/mis-cased PR row can no longer silently skip the gate. A genuinely
    unresolvable ticket (no PR row, no CLEAR) is a no-op (nothing to gate); a resolved
    directive ticket runs the fail-closed gate.
    """
    ticket = resolve_gated_ticket(slug=slug, pr_id=pr_id)
    if ticket is None:
        return
    check_merge_quality_verdict(ticket, head_sha)
