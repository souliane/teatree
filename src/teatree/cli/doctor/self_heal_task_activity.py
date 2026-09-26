import datetime as dt
from collections.abc import Callable
from dataclasses import dataclass

import typer

_INACTIVITY_SECONDS = 900


@dataclass(frozen=True, slots=True)
class _TaskAttemptActivity:
    active_task_count: int
    latest_activity_at: dt.datetime | None
    has_live_attempt: bool


def _active_task_attempt_activity() -> _TaskAttemptActivity:
    from django.db.models import Count, Max, Q  # noqa: PLC0415 — deferred: keeps CLI startup light

    from teatree.core.models import Task, TaskAttempt  # noqa: PLC0415 — deferred: keeps CLI startup light

    active = Task.objects.filter(status__in=Task.Status.active())
    task_summary = active.aggregate(count=Count("pk"), newest=Max("created_at"))
    if not task_summary["count"]:
        return _TaskAttemptActivity(0, None, False)
    attempt_summary = TaskAttempt.objects.filter(task__in=active).aggregate(
        live=Count("pk", filter=Q(ended_at__isnull=True)),
        newest_end=Max("ended_at"),
    )
    candidates = [value for value in (task_summary["newest"], attempt_summary["newest_end"]) if value is not None]
    return _TaskAttemptActivity(
        active_task_count=int(task_summary["count"]),
        latest_activity_at=max(candidates) if candidates else None,
        has_live_attempt=bool(attempt_summary["live"]),
    )


def check_task_attempt_activity(*, now: dt.datetime, runner_on: Callable[[], bool]) -> bool:
    try:
        if not runner_on():
            return True
        activity = _active_task_attempt_activity()
    except Exception as exc:  # noqa: BLE001 — a self-heal probe must never crash the doctor run
        typer.echo(f"WARN  Task-attempt activity check crashed: {exc.__class__.__name__}: {exc}")
        return True
    if activity.active_task_count == 0 or activity.latest_activity_at is None or activity.has_live_attempt:
        return True
    age_seconds = int((now - activity.latest_activity_at).total_seconds())
    if age_seconds <= _INACTIVITY_SECONDS:
        return True
    typer.echo(
        f"FAIL  the active preset admits work with {activity.active_task_count} active task(s), but no task attempt "
        f"has run for {age_seconds // 60} min (threshold "
        f"{_INACTIVITY_SECONDS // 60} min) — work is queued but nothing is running."
    )
    return False
