"""Owner receipts for the periodic architectural review."""

from teatree.core.modelkit.notify_policy import NotifyAudience
from teatree.core.models.task import Task
from teatree.core.notify import NotifyKind, notify_user
from teatree.url_classify import first_pr_url


def announce_completed(task: Task, summary: str) -> None:
    url = first_pr_url(summary)
    outcome = f"PR: {url}" if url else "No pull request URL was reported."
    notify_user(
        f"Architectural review for {task.ticket.overlay} completed. {outcome}",
        kind=NotifyKind.INFO,
        audience=NotifyAudience.OWNER_DELIVERY,
        idempotency_key=f"architectural-review:completed:{task.pk}",
    )


def announce_failed(task: Task, reason: str) -> None:
    notify_user(
        f"Architectural review for {task.ticket.overlay} failed: {reason.rstrip('.')}.",
        kind=NotifyKind.INFO,
        audience=NotifyAudience.OWNER_ESCALATION,
        idempotency_key=f"architectural-review:failed:{task.pk}",
    )


def announce_held(task: Task, reason: str, *, hold_kind: str) -> None:
    notify_user(
        f"Architectural review for {task.ticket.overlay} is held: {reason}.",
        kind=NotifyKind.INFO,
        audience=NotifyAudience.OWNER_ESCALATION,
        idempotency_key=f"architectural-review:held:{task.pk}:{hold_kind}",
    )
