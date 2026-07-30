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

Which tickets it gates: DIRECTIVE tickets UNCONDITIONALLY (self-modification is
held to the stricter bar — the machine gets no benefit of the doubt); ORDINARY
tickets only when the per-overlay ``require_merge_quality_verdict`` flag is on
(DARK, default off) — so ordinary work merges unchanged until an overlay opts in.
Its own kill-switch (setting the flag back off) is the audited never-lockout
escape for ordinary tickets; a directive ticket's escape is a corrected+re-judged
verdict at the shipped head.

Wired at :func:`~teatree.core.merge.execution.execute_bound_merge` — the single
chokepoint BOTH autonomous merge paths cross, mirroring ``assert_review_verdict_gate``.
"""

import logging
from typing import TYPE_CHECKING, cast

from teatree.config import get_effective_settings
from teatree.core.gates.plan_currency_gate import latest_plan_artifact
from teatree.core.merge.errors import MergePreconditionError
from teatree.core.models.critic_dispatch import CriticDispatch
from teatree.core.models.critic_finding import CriticFinding, CriticFindingSpec
from teatree.core.models.critic_verdict import CriticVerdict
from teatree.core.models.directive import Directive
from teatree.core.models.merge_clear import MergeClear
from teatree.core.models.pull_request import PullRequest
from teatree.core.review.critic_rubric import _MERGE_TRANSITION, item_for, llm_items

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


def merge_quality_enforced(ticket: "Ticket") -> bool:
    """Whether the merge-quality verdict BLOCKS this ticket's merge.

    Directive tickets: always (the stricter self-modification bar). Ordinary
    tickets: only under the per-overlay ``require_merge_quality_verdict`` flag.
    """
    if is_directive_ticket(ticket):
        return True
    return bool(get_effective_settings(ticket.overlay or None).require_merge_quality_verdict)


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
    questions = "\n".join(f"  - {item.slug}: {item.adversarial_question}" for item in llm_items(_MERGE_TRANSITION))
    item_slugs = ", ".join(item.slug for item in llm_items(_MERGE_TRANSITION))
    strategy = ratified_test_strategy(ticket)
    return (
        f"You are the merge-quality CRITIC deciding whether the SHIPPED change on ticket {ticket.pk} at head "
        f"{head_sha[:8] or '<unknown>'} is not merely green but WELL-ENGINEERED — clean and tested-enough WITHOUT "
        f"bloat. Read the diff and the touched modules, then answer each item against the ARTIFACTS.\n\n"
        f"Anchor for `test_value` — {strategy}\n\n"
        f"`test_value` is BOTH directions: a VACUOUS test (asserts nothing that could fail — a tautology, the "
        f"framework, a mock talking to a mock) FAILS, AND a BLOATED test set (redundant permutations beyond the "
        f"ratified strategy, same behavior/same failure mode in different dressing) FAILS; the coverage-that-matters "
        f"from the strategy must be present with no undeclared redundant tests. `cleanliness` is the CLAUDE.md bar: "
        f"full typing (no smuggled Any), composition over inheritance, Django conventions, self-documenting names, "
        f"no parallel mechanism where a substrate convention exists, docs/BLUEPRINT left consistent.\n\n"
        f"Questions:\n{questions}\n\nRETURN your judgment in the result envelope (the phase has no shell — the "
        f'orchestrator records it): `"critic_verdict": {{"grader_identity": "<your-critic-id>", "items": '
        f'[{{"slug": "<one of {item_slugs}>", "status": "pass"|"fail", "citation": "<test node id or file:line '
        f'naming WHY — an uncited pass records as a FAIL>"}}]}}`. One item per question — judge BOTH.'
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
    passed = {item.slug for item in verdict.item_verdicts() if not item.is_fail()}
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
    """Refuse the merge unless a clean merge-quality verdict covers *head_sha* (when enforced).

    NO-OP when the ticket is not gated (ordinary ticket, flag off). Otherwise
    fail-closed: no covering verdict → arm the async critic and refuse; a verdict
    with any unmet merge item → record the FAIL findings and refuse; every item
    affirmatively passed → proceed.
    """
    if not merge_quality_enforced(ticket):
        return
    head = head_sha.strip().lower()
    verdict = covering_verdict(ticket, head)
    if verdict is None:
        _arm_merge_quality_critic(ticket, head)
        msg = (
            f"no recorded merge-quality CriticVerdict covers the shipped head {head} for ticket {ticket.pk} — "
            f"refusing to merge (north-star PR-4). A clean-and-tested-enough verdict (test_value + cleanliness) at "
            f"the exact head is required before a directive keystone merges; a headless critic has been armed and "
            f"the merge can proceed once it records a clean verdict (same as CI pending). Ordinary tickets are gated "
            f"only under `require_merge_quality_verdict`; disabling it per-overlay is the audited never-lockout escape."
        )
        raise MergeQualityVerdictError(msg)
    record_merge_quality_findings(verdict, ticket=ticket, head_sha=head)
    unmet = unmet_items(verdict)
    if unmet:
        msg = (
            f"the merge-quality CriticVerdict at head {head} for ticket {ticket.pk} does not clear "
            f"{', '.join(unmet)} — refusing to merge (north-star PR-4): merely-green is not well-engineered. Each "
            f"unmet item is recorded as a CriticFinding naming the offending test/file — resolve them and re-judge "
            f"at the shipped head. The never-lockout escape for an ordinary ticket is disabling "
            f"`require_merge_quality_verdict` per-overlay."
        )
        raise MergeQualityVerdictError(msg)


def _resolve_gated_ticket(*, slug: str, pr_id: int) -> "Ticket | None":
    """The ticket whose merge quality is gated, or ``None`` when nothing is gated.

    Resolved from the PR ledger ``(repo, iid)`` FIRST (case-insensitive — GitHub
    slugs are case-insensitive, so a ``Owner/Repo`` row must still match an
    ``owner/repo`` merge). When the PR ledger has no matching row (never recorded,
    or mis-cased before this fix), fall back to the ``MergeClear`` for the same
    ``(slug, pr_id)`` — the CLEAR carries the ticket FK at merge time and is the
    reliable handle. This closes the silent-bypass: a directive PR whose PR-ledger
    row is missing or mis-cased no longer skips the PR-4 gate, because its CLEAR's
    ticket still resolves and the (fail-closed) gate runs.
    """
    pr = PullRequest.objects.filter(repo__iexact=slug, iid=str(pr_id)).select_related("ticket").order_by("-id").first()
    if pr is not None and pr.ticket is not None:
        return pr.ticket
    clear = MergeClear.objects.filter(slug__iexact=slug, pr_id=pr_id).select_related("ticket").order_by("-id").first()
    if clear is not None and clear.ticket is not None:
        return clear.ticket
    return None


def assert_merge_quality_verdict(*, slug: str, pr_id: int, head_sha: str) -> None:
    """The ``execute_bound_merge`` chokepoint entry — resolve the ticket then run the gate.

    Resolves the gated ticket from the PR ``(repo, iid)`` ledger (case-insensitive)
    OR the ``MergeClear`` for the same ``(slug, pr_id)`` (:func:`_resolve_gated_ticket`),
    so a missing/mis-cased PR row can no longer silently skip the gate. A genuinely
    unresolvable ticket (no PR row, no CLEAR) is a no-op (nothing to gate); a resolved
    directive ticket runs the fail-closed gate.
    """
    ticket = _resolve_gated_ticket(slug=slug, pr_id=pr_id)
    if ticket is None:
        return
    check_merge_quality_verdict(ticket, head_sha)
