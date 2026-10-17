"""Read the latest cancellation issuer from a task's immutable attempt history."""

from teatree.core.models import Task, TaskAttempt


def cancel_source(task: Task) -> str:
    attempt = TaskAttempt.objects.filter(task=task, result__has_key="cancel_source").order_by("-pk").first()
    return str(attempt.result.get("cancel_source") or "") if attempt is not None else ""
