"""Owner waits and internal escalation counts for checking reports."""

from teatree.core.checking_types import OwnerWaitItem
from teatree.core.models.deferred_question import DeferredQuestion
from teatree.core.waiting import WaitingKind, format_age, gather_waiting


def owner_wait_items(*, overlay_slug: str, overlay: str = "") -> list[OwnerWaitItem]:
    """Every live owner wait, oldest first, from the shared waiting lane."""
    waits = sorted(gather_waiting(overlay), key=lambda entry: entry.age, reverse=True)
    items: list[OwnerWaitItem] = []
    for wait in waits:
        question = wait.kind == WaitingKind.QUESTION
        label = f"Q{wait.row_id}: {wait.ref}" if question else wait.ref
        if not overlay and wait.overlay:
            label = f"[{wait.overlay}] {label}"
        detail = (
            "awaiting your Slack reply"
            if question and wait.owner_only
            else (
                f"answer in its Slack thread, or t3 {overlay_slug} questions dismiss {wait.row_id}"
                " --reason '<evidence>'"
            )
            if question
            else f"waiting {format_age(wait.age)}"
        )
        items.append(
            OwnerWaitItem(
                label=label,
                url=wait.url,
                detail=detail,
                kind=wait.kind,
                age_days=max(0, wait.age.days),
                row_id=wait.row_id,
                owner_only=wait.owner_only,
            )
        )
    return items


def internal_escalation_count() -> int:
    return DeferredQuestion.internal_pending().count()
