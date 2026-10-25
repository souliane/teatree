"""PR sweep ordering and durable status reporting."""

from collections.abc import Callable
from datetime import UTC, datetime

from teatree.core.review.pr_inventory import PrInventoryEntry
from teatree.core.review.pr_next_action import UNKNOWN, approvals_text, ci_label, sweep_next_task, threads_label
from teatree.core.review.review_candidate import author_is_self
from teatree.loop.scanners.base import ScannerError, ScannerErrorClass, ScanSignal
from teatree.loop.scanners.pr_sweep_types import MergeAttempt, PrSummary


def pr_created_at(pr: PrSummary) -> datetime:
    try:
        created = datetime.fromisoformat(pr.created_at)
    except ValueError:
        return datetime.max.replace(tzinfo=UTC)
    return created.astimezone(UTC) if created.tzinfo is not None else datetime.max.replace(tzinfo=UTC)


def write_inventory_for_scan(
    writer: Callable[..., object] | None,
    overlay: str,
    entries: list[PrInventoryEntry],
    repos: tuple[str, ...],
    errors: list[ScannerError],
) -> None:
    if writer is None:
        return
    try:
        writer(overlay, entries, watched_repos=repos)
    except (OSError, ValueError) as exc:
        errors.append(
            ScannerError(
                scanner="pr_sweep", error_class=ScannerErrorClass.UNKNOWN, detail=f"inventory write failed: {exc}"
            )
        )


def inventory_entry(
    pr: PrSummary, attempt: MergeAttempt | None, *, identities: tuple[str, ...] = ()
) -> PrInventoryEntry:
    """The PR's inventory row; *attempt* is ``None`` when the sweep could not evaluate it."""
    reason = attempt.reason if attempt else "evaluation_failed"
    return PrInventoryEntry(
        url=pr.url,
        repo=pr.slug,
        iid=pr.number,
        title=pr.title,
        created_at=pr.created_at,
        updated_at=pr.updated_at,
        next_task=(
            sweep_next_task(reason, attempt.decision, behind_main=pr.behind_main)
            if attempt
            else "Inspect why the PR sweep could not evaluate this PR"
        ),
        decision=attempt.decision if attempt else "evaluation_failed",
        ci=ci_label(reason, failing_required=bool(attempt and attempt.failing_required)),
        approvals=pr.approvals,
        approvals_display=approvals_text(pr.approvals, pr.approval_count, None),
        threads=threads_label(pr.unresolved_threads) if pr.unresolved_threads is not None else UNKNOWN,
        draft=pr.is_draft,
        reason=reason,
        head_sha=pr.head_sha,
        author=pr.author,
        owned=(
            author_is_self(
                pr.author.casefold(), current_user="", self_identities=(name.casefold() for name in identities)
            )
            if pr.author and identities
            else None
        ),
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
