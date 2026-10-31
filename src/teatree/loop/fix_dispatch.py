"""What a fix dispatch tells its agent, and when a fix dispatch has already been overtaken by a push."""

import logging

from teatree.agents.fixer_brief import FixerBrief
from teatree.core import on_behalf_gate_recorded
from teatree.core.merge.ci_rollup import CodeHostQuery
from teatree.core.models import RedMrFixAttempt
from teatree.core.models.ticket_scheduling import with_reread_first
from teatree.loop.dispatch_gates import claim_red_mr_fix, fix_kind_of
from teatree.loop.dispatch_tables import ActionPayload
from teatree.utils.url_slug import pr_ref_from_url

logger = logging.getLogger(__name__)

_REVIEW_FINDINGS_BASE = (
    "Auto-scheduled review-findings fix — read unresolved discussions on {pr_url} and "
    "recorded findings via t3 <overlay> review findings <pr-url>, plus prior merge-critic "
    "findings in the coding brief. IMPLEMENT them on that "
    "PR's own branch, run the tests and push once. "
)
_VOICED_REVIEW_FINDINGS_REASON = _REVIEW_FINDINGS_BASE + (
    "On GitLab, answer questions and push-backs in their own thread; "
    "reply only after the push has landed with 'Fixed in <short sha>.' to each fixed thread and resolve it. "
    "Skip any thread whose latest reply is already ours. "
    "Answer, reply and resolve only via t3 review reply-to-discussion and t3 review resolve-discussion; "
    "if either refuses, post nothing by another route. "
    "On a GitHub PR there is no sanctioned reply/resolve path: fix and push only. "
    "Never add a top-level comment or change reviewers; escalate only if the fix cannot proceed"
)

#: The review-findings entry is the quiet default; the conflict entry forbids rebasing.
FIX_REASON_BY_KIND: dict[str, str] = {
    RedMrFixAttempt.Kind.CI_RED: "Auto-scheduled red-MR fix — debug {pr_url}",
    RedMrFixAttempt.Kind.MERGE_CONFLICT: (
        "Auto-scheduled merge-conflict fix — resolve {pr_url} by MERGING the target branch "
        "into the head branch, never by rebasing"
    ),
    # A quiet fix must leave review threads unresolved as well as unanswered.
    RedMrFixAttempt.Kind.REVIEW_FINDINGS: _REVIEW_FINDINGS_BASE
    + (
        "Never reply to or resolve a thread. "
        "Never answer a finding on our own MR with another comment. "
        "Escalate only if the fix cannot proceed"
    ),
}


_CONFLICT_ESCAPE = (
    "when the merge cannot be completed, end the task with needs_user_input naming the conflicting paths; "
    "never rebase, force-push or resolve on the target branch"
)


def fix_reason(payload: ActionPayload, pr_url: str) -> str:
    fix_kind = fix_kind_of(payload)
    reason = FIX_REASON_BY_KIND[fix_kind]
    if fix_kind == RedMrFixAttempt.Kind.REVIEW_FINDINGS and on_behalf_gate_recorded.owner_voice_permitted():
        reason = _VOICED_REVIEW_FINDINGS_REASON
    reason = reason.format(pr_url=pr_url)
    if fix_kind == RedMrFixAttempt.Kind.REVIEW_FINDINGS:
        return with_reread_first(reason)
    if fix_kind != RedMrFixAttempt.Kind.MERGE_CONFLICT:
        return reason
    return FixerBrief(
        pr_url=pr_url,
        head_sha=str(payload.get("head_sha") or ""),
        base_sha=str(payload.get("base_sha") or ""),
        agent_type="debugging",
        escape=_CONFLICT_ESCAPE,
        checkout_kind="ticket worktree on the PR head branch",
        findings=(reason,),
    ).render()


def settle_superseded_fix(payload: ActionPayload, pr_url: str) -> bool:
    """Claim the scanned head's slot and return True when the forge's live head is no longer the scanned one.

    An unreadable forge or an unrecognised URL names no live head, so it never supersedes a fix.
    """
    scanned = str(payload.get("head_sha") or "").lower()
    live = _live_head(pr_url).lower()
    if not scanned or not live or live.startswith(scanned) or scanned.startswith(live):
        return False
    claim_red_mr_fix(payload)
    logger.info("Fix for %s superseded: scanned head %s, live head %s", pr_url, scanned[:12], live[:12])
    return True


def _live_head(pr_url: str) -> str:
    ref = pr_ref_from_url(pr_url)
    if ref is None:
        return ""
    try:
        return CodeHostQuery.for_ref(ref).live_head_read().sha
    except Exception:
        logger.exception("Live head read failed for %s; the fix proceeds on the scanned head", pr_url)
        return ""
