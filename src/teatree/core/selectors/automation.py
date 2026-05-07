from django.db.models import Q, Sum
from django.utils import timezone

from teatree.core.models import Task, TaskAttempt, Ticket

from ._filters import _overlay_q, _task_overlay_q
from ._types import ActionRequiredItem, AutomationSummary, DiscussionData, ReviewCommentDetail

_AUTOMATION_WINDOW_HOURS = 24


def build_automation_summary(overlay: str | None = None) -> AutomationSummary:
    cutoff = timezone.now() - timezone.timedelta(hours=_AUTOMATION_WINDOW_HOURS)
    task_filter = Q(
        execution_target=Task.ExecutionTarget.HEADLESS,
        status=Task.Status.CLAIMED,
    )
    if overlay:
        task_filter &= _overlay_q(overlay)
    running = Task.objects.filter(task_filter).count()
    attempt_filter = Q(
        task__execution_target=Task.ExecutionTarget.HEADLESS,
        ended_at__gte=cutoff,
    ) & _task_overlay_q(overlay)
    recent_attempts = TaskAttempt.objects.filter(attempt_filter)
    completed_24h = recent_attempts.count()
    succeeded_24h = recent_attempts.filter(exit_code=0).count()
    failed_24h = completed_24h - succeeded_24h
    token_stats = recent_attempts.aggregate(
        total_input=Sum("input_tokens"),
        total_output=Sum("output_tokens"),
        total_cost=Sum("cost_usd"),
    )
    total_tokens_24h = (token_stats["total_input"] or 0) + (token_stats["total_output"] or 0)
    total_cost_24h = token_stats["total_cost"] or 0.0
    last_attempt = (
        TaskAttempt.objects.filter(
            Q(task__execution_target=Task.ExecutionTarget.HEADLESS, ended_at__isnull=False) & _task_overlay_q(overlay),
        )
        .order_by("-ended_at")
        .first()
    )
    last_completed_at = last_attempt.ended_at.isoformat() if last_attempt else ""
    return AutomationSummary(
        running=running,
        completed_24h=completed_24h,
        succeeded_24h=succeeded_24h,
        failed_24h=failed_24h,
        last_completed_at=last_completed_at,
        total_tokens_24h=total_tokens_24h,
        total_cost_24h=total_cost_24h,
    )


def build_action_required(overlay: str | None = None) -> list[ActionRequiredItem]:
    """Aggregate all items that need human attention."""
    task_qs = Task.objects.filter(
        execution_target=Task.ExecutionTarget.INTERACTIVE,
        status=Task.Status.PENDING,
    ).select_related("ticket")
    if overlay:
        task_qs = task_qs.filter(_overlay_q(overlay))
    items: list[ActionRequiredItem] = [
        ActionRequiredItem(
            kind="interactive_task",
            label=f"#{task.ticket.ticket_number} — interactive task",
            url="",
            ticket_id=task.ticket_id,
            detail=task.execution_reason[:120],
        )
        for task in task_qs
    ]

    items.extend(_action_items_from_prs(overlay))
    return items


def _action_items_from_prs(overlay: str | None = None) -> list[ActionRequiredItem]:
    """Scan in-flight PRs for review/approval needs."""
    items: list[ActionRequiredItem] = []
    for ticket in Ticket.objects.in_flight(overlay=overlay):
        extra = ticket.extra if isinstance(ticket.extra, dict) else {}
        prs = extra.get("prs", {})
        if not isinstance(prs, dict):
            continue
        for pr in prs.values():
            items.extend(_check_pr(pr, ticket))
    return items


_DISCUSSION_STATUS_DISPLAY = {
    "waiting_reviewer": "Waiting reviewer",
    "needs_reply": "Needs reply",
    "addressed": "Addressed",
}


def _check_pr(pr: dict, ticket: "Ticket") -> list[ActionRequiredItem]:
    """Return action items for a single PR dict."""
    if not isinstance(pr, dict) or pr.get("draft") or pr.get("state") in {"merged", "closed"}:
        return []
    repo = str(pr.get("repo", ""))
    iid = str(pr.get("iid", ""))
    pr_url = str(pr.get("url", ""))
    pr_label = f"{repo} #{iid}"
    pipeline = pr.get("pipeline_status")
    slack_url = str(pr.get("review_permalink", ""))
    approvals = pr.get("approvals", {})
    if not isinstance(approvals, dict):
        approvals = {}
    count = int(approvals.get("count", 0))
    required = int(approvals.get("required", 1))
    items: list[ActionRequiredItem] = []

    if pipeline == "success" and not pr.get("review_permalink") and not pr.get("review_requested"):
        items.append(
            ActionRequiredItem(
                kind="needs_review_request",
                label=f"{pr_label} — ready for review request",
                url=pr_url,
                ticket_id=ticket.pk,
                detail="CI green, no review posted yet",
            ),
        )

    raw_discussions = pr.get("discussions", [])
    discussions: list[DiscussionData] = (
        [d for d in raw_discussions if isinstance(d, dict)] if isinstance(raw_discussions, list) else []
    )
    comment_details = _extract_review_comments(discussions)
    needs_reply = sum(1 for c in comment_details if c.status == "Needs reply")
    if needs_reply:
        items.append(
            ActionRequiredItem(
                kind="needs_reply",
                label=f"{pr_label} — {needs_reply} comment{'s' if needs_reply > 1 else ''} need reply",
                url=pr_url,
                ticket_id=ticket.pk,
                detail="Review threads waiting for your response",
                slack_url=slack_url,
                review_comments=tuple(comment_details),
            ),
        )

    if pipeline == "success" and pr.get("review_requested") and count < required:
        items.append(
            ActionRequiredItem(
                kind="needs_approval",
                label=f"{pr_label} — waiting for approval ({count}/{required})",
                url=pr_url,
                ticket_id=ticket.pk,
                detail="Review requested, approval pending",
                slack_url=slack_url,
            ),
        )

    draft_count = pr.get("draft_comments_count")
    if pr.get("draft_comments_pending") and isinstance(draft_count, int) and draft_count > 0:
        items.append(
            ActionRequiredItem(
                kind="review_draft",
                label=f"{pr_label} — agent posted review comments",
                url=pr_url,
                ticket_id=ticket.pk,
                detail=f"{draft_count} draft comment{'s' if draft_count > 1 else ''}"
                " need your review before publishing",
            ),
        )

    return items


def _extract_review_comments(discussions: list[DiscussionData]) -> tuple[ReviewCommentDetail, ...]:
    """Extract review comment details from PR discussion data."""
    comments: list[ReviewCommentDetail] = []
    for disc in discussions:
        status_key = str(disc.get("status", ""))
        comments.append(
            ReviewCommentDetail(
                status=_DISCUSSION_STATUS_DISPLAY.get(status_key, status_key),
                detail_text=str(disc.get("detail", ""))[:120],
            ),
        )
    return tuple(comments)
