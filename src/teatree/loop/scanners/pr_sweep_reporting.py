"""PR sweep ordering and durable status reporting."""

from datetime import UTC, datetime

from teatree.core.pr_inventory import PrInventoryEntry
from teatree.loop.scanners.base import ScanSignal
from teatree.loop.scanners.pr_sweep_types import (
    CLEAR_PRESENT_UNUSABLE_REASON,
    MERGEABLE_AWAITING_REVIEW_REASON,
    MergeAttempt,
    PrSummary,
)


def pr_created_at(pr: PrSummary) -> datetime:
    try:
        created = datetime.fromisoformat(pr.created_at)
    except ValueError:
        return datetime.max.replace(tzinfo=UTC)
    return created.astimezone(UTC) if created.tzinfo is not None else datetime.max.replace(tzinfo=UTC)


def next_task_for_attempt(pr: PrSummary, attempt: MergeAttempt) -> str:
    if attempt.reason == "no_clear_for_head":
        return (
            "Update PR branch against main" if pr.behind_main else "Obtain independent review and merge authorization"
        )
    actions = {
        "draft": "Finish draft and request review",
        "changes_requested": "Address requested changes",
        "ci_red": "Fix failing CI",
        "ci_pending": "Wait for CI to finish",
        "required_checks_indeterminate": "Inspect required checks",
        "conflict": "Resolve merge conflict",
        "solo_overlay_no_review": "Run independent review",
        CLEAR_PRESENT_UNUSABLE_REASON: "Refresh merge authorization for current head",
        MERGEABLE_AWAITING_REVIEW_REASON: "Request independent review",
    }
    return actions.get(attempt.reason, f"Inspect PR blocker: {(attempt.reason or attempt.decision).replace('_', ' ')}")


def inventory_entry(pr: PrSummary, attempt: MergeAttempt) -> PrInventoryEntry:
    return PrInventoryEntry(
        url=pr.url,
        repo=pr.slug,
        iid=pr.number,
        title=pr.title,
        created_at=pr.created_at,
        updated_at=pr.updated_at,
        next_task=next_task_for_attempt(pr, attempt),
        decision=attempt.decision,
        ci="red" if attempt.failing_required else "unknown",
        approvals="unknown",
        threads="unknown",
        draft=pr.is_draft,
        reason=attempt.reason,
    )


def signal_from_attempt(attempt: MergeAttempt, *, overlay: str) -> ScanSignal:
    return ScanSignal(
        kind="pr_sweep.merged" if attempt.merged else f"pr_sweep.{attempt.decision}",
        summary=f"{attempt.slug}#{attempt.pr_id} {attempt.decision} ({attempt.reason})",
        payload={
            "slug": attempt.slug,
            "pr_id": attempt.pr_id,
            "decision": attempt.decision,
            "reason": attempt.reason,
            "merged": attempt.merged,
            "merged_sha": attempt.merged_sha,
            "overlay": overlay,
            "url": attempt.url,
            "review_dispatched": attempt.review_dispatched,
            "failing_required": list(attempt.failing_required),
            "base_current": attempt.base_current,
            "held_verdicts": [list(ref) for ref in attempt.held_verdicts],
            "authorizing_verdict": None if attempt.authorizing_verdict is None else list(attempt.authorizing_verdict),
        },
    )
