"""Read the latest cancellation issuer from a task's immutable attempt history."""

from collections.abc import Iterable, Mapping
from datetime import datetime

from teatree.core.models import Task, TaskAttempt

CANCEL_SOURCE_KEY = "cancel_source"


def cancel_source(task: Task) -> str:
    attempt = TaskAttempt.objects.filter(task=task, result__has_key=CANCEL_SOURCE_KEY).order_by("-pk").first()
    return str(attempt.result.get(CANCEL_SOURCE_KEY) or "") if attempt is not None else ""


def count_recorded_cancels(
    rows: Iterable[tuple[datetime | None, Mapping[str, object]]], *, start: datetime | None, merged_at: datetime
) -> int:
    if start is None:
        return 0
    return sum(
        bool(result.get(CANCEL_SOURCE_KEY))
        for ended, result in rows
        if ended is not None and start <= ended <= merged_at
    )
