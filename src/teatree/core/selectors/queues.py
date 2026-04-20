from django.db.models import Max
from django.utils import timezone

from teatree.core.models import Task, TaskAttempt

from ._filters import _overlay_q
from ._helpers import _humanize_duration
from ._types import DashboardTaskRow

_HIDDEN_STATUSES = (Task.Status.COMPLETED, Task.Status.FAILED)


def _last_error_for_tasks(task_ids: list[int]) -> dict[int, str]:
    """Return the most recent non-empty error per task from TaskAttempt."""
    latest_ids = (
        TaskAttempt.objects.filter(task_id__in=task_ids, error__gt="")
        .values("task_id")
        .annotate(latest_pk=Max("pk"))
        .values_list("latest_pk", flat=True)
    )
    attempts = TaskAttempt.objects.filter(pk__in=latest_ids).values_list("task_id", "error")
    return dict(attempts)


def _last_result_for_tasks(task_ids: list[int]) -> dict[int, str]:
    latest_ids = (
        TaskAttempt.objects.filter(task_id__in=task_ids)
        .exclude(result={})
        .values("task_id")
        .annotate(latest_pk=Max("pk"))
        .values_list("latest_pk", flat=True)
    )
    result: dict[int, str] = {}
    for attempt in TaskAttempt.objects.filter(pk__in=latest_ids):
        data = attempt.result if isinstance(attempt.result, dict) else {}
        summary = str(data.get("summary", ""))
        if summary:
            result[attempt.task_id] = summary
    return result


def _build_task_queue(
    target: str,
    *,
    include_dismissed: bool = False,
    pending_only: bool = False,
    overlay: str | None = None,
) -> list[DashboardTaskRow]:
    Task.objects.reap_stale_claims()
    qs = Task.objects.filter(execution_target=target).select_related("ticket", "session")
    if overlay:
        qs = qs.filter(_overlay_q(overlay))
    qs = qs.order_by("pk")
    if pending_only:
        qs = qs.filter(status=Task.Status.PENDING)
    elif not include_dismissed:
        qs = qs.exclude(status__in=_HIDDEN_STATUSES)
    else:
        qs = qs.exclude(status=Task.Status.COMPLETED)
    tasks = qs
    task_list = list(tasks)
    ids = [t.pk for t in task_list]
    errors = _last_error_for_tasks(ids)
    results = _last_result_for_tasks(ids)
    now = timezone.now()
    return [
        DashboardTaskRow(
            task_id=task.pk,
            ticket_id=task.ticket_id,
            ticket_display_id=task.ticket.ticket_number,
            execution_reason=task.execution_reason,
            status=task.get_status_display(),
            claimed_by=task.claimed_by,
            last_error=errors.get(task.pk, ""),
            result_summary=results.get(task.pk, ""),
            session_agent_id=task.session.agent_id if task.session_id else "",
            phase=task.phase,
            issue_url=task.ticket.issue_url,
            elapsed_time=_humanize_duration((now - task.claimed_at).total_seconds()) if task.claimed_at else "",
            heartbeat_age=_humanize_duration((now - task.heartbeat_at).total_seconds()) if task.heartbeat_at else "",
        )
        for task in task_list
    ]


def build_headless_queue(*, include_dismissed: bool = False, overlay: str | None = None) -> list[DashboardTaskRow]:
    return _build_task_queue(Task.ExecutionTarget.HEADLESS, include_dismissed=include_dismissed, overlay=overlay)


def build_interactive_queue(
    *,
    include_dismissed: bool = False,
    pending_only: bool = False,
    overlay: str | None = None,
) -> list[DashboardTaskRow]:
    return _build_task_queue(
        Task.ExecutionTarget.INTERACTIVE,
        include_dismissed=include_dismissed,
        pending_only=pending_only,
        overlay=overlay,
    )
