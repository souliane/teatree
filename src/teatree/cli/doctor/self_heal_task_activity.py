import datetime as dt
from collections.abc import Callable

import typer

_INACTIVITY_SECONDS = 900


def _active_task_attempt_activity() -> tuple[int, dt.datetime | None]:
    from django.db.models import Count, Max  # noqa: PLC0415 — deferred: keeps CLI startup light

    from teatree.core.models import Task, TaskAttempt  # noqa: PLC0415 — deferred: keeps CLI startup light

    active = Task.objects.filter(status__in=Task.Status.active())
    task_summary = active.aggregate(count=Count("pk"), newest=Max("created_at"))
    if not task_summary["count"]:
        return 0, None
    newest_attempt = TaskAttempt.objects.filter(task__in=active).aggregate(newest=Max("started_at"))["newest"]
    candidates = [value for value in (task_summary["newest"], newest_attempt) if value is not None]
    return int(task_summary["count"]), max(candidates) if candidates else None


def check_task_attempt_activity(*, now: dt.datetime, runner_on: Callable[[], bool]) -> bool:
    try:
        if not runner_on():
            return True
        active_count, last_activity = _active_task_attempt_activity()
    except Exception as exc:  # noqa: BLE001 — a self-heal probe must never crash the doctor run
        typer.echo(f"WARN  Task-attempt activity check crashed: {exc.__class__.__name__}: {exc}")
        return True
    if active_count == 0 or last_activity is None:
        return True
    age_seconds = int((now - last_activity).total_seconds())
    if age_seconds <= _INACTIVITY_SECONDS:
        return True
    typer.echo(
        f"FAIL  loop_runner_enabled is ON with {active_count} active task(s), but no task attempt "
        f"has started for {age_seconds // 60} min (threshold "
        f"{_INACTIVITY_SECONDS // 60} min) — work is queued but nothing is running."
    )
    return False
