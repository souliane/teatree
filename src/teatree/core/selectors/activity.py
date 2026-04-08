import json
import os

from teatree.core.models import Task, TaskAttempt, Ticket

from ._filters import _task_overlay_q
from ._helpers import _CLAUDE_SESSIONS_DIR, _uptime_from_epoch_ms
from ._types import ActiveSessionRow, DashboardReviewCommentRow, RecentActivityRow

_DISCUSSION_STATUS_DISPLAY = {
    "waiting_reviewer": "Waiting reviewer",
    "needs_reply": "Needs reply",
    "addressed": "Addressed",
}

_RECENT_ACTIVITY_LIMIT = 10


def build_active_sessions() -> list[ActiveSessionRow]:
    """Discover active claude sessions from ~/.claude/sessions/ files."""
    if not _CLAUDE_SESSIONS_DIR.is_dir():
        return []

    active_statuses = (Task.Status.PENDING, Task.Status.CLAIMED)
    active_tasks = {t.pk: t for t in Task.objects.filter(status__in=active_statuses).select_related("ticket")}

    # Match tasks to sessions by agent_session_id
    session_to_task: dict[str, Task] = {}
    for task in active_tasks.values():
        last_attempt = task.attempts.order_by("-pk").first()
        if last_attempt and last_attempt.agent_session_id:
            session_to_task[last_attempt.agent_session_id] = task

    # Collect session IDs for finished tasks so we can exclude them
    finished_statuses = (Task.Status.COMPLETED, Task.Status.FAILED)
    finished_session_ids: set[str] = set(
        TaskAttempt.objects.filter(
            task__status__in=finished_statuses,
        )
        .exclude(agent_session_id="")
        .values_list("agent_session_id", flat=True)
    )

    sessions: list[ActiveSessionRow] = []
    for session_file in _CLAUDE_SESSIONS_DIR.glob("*.json"):
        try:
            data = json.loads(session_file.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            continue

        pid = data.get("pid")
        if not isinstance(pid, int):
            continue

        # Check if process is still running
        try:
            os.kill(pid, 0)
        except (ProcessLookupError, PermissionError):
            continue

        session_id = str(data.get("sessionId", ""))
        if session_id and session_id in finished_session_ids:
            continue
        task = session_to_task.get(session_id)

        sessions.append(
            ActiveSessionRow(
                pid=pid,
                uptime=_uptime_from_epoch_ms(data.get("startedAt", 0)) if data.get("startedAt") else "",
                kind="headless" if task and task.execution_target == Task.ExecutionTarget.HEADLESS else "interactive",
                task_id=task.pk if task else None,
                ticket_id=task.ticket.pk if task else None,
                phase=task.phase if task else "",
                launch_url="",
                session_id=session_id,
                cwd=str(data.get("cwd", "")),
                name=str(data.get("name", "")),
            ),
        )

    return sessions


def build_review_comments(overlay: str | None = None) -> list[DashboardReviewCommentRow]:
    rows: list[DashboardReviewCommentRow] = []
    for ticket in Ticket.objects.in_flight(overlay=overlay):
        extra = ticket.extra if isinstance(ticket.extra, dict) else {}
        mrs_data = extra.get("mrs", {})
        if not isinstance(mrs_data, dict):
            continue
        for mr in mrs_data.values():
            if not isinstance(mr, dict):
                continue
            discussions = mr.get("discussions", [])
            if not isinstance(discussions, list):
                continue
            repo = str(mr.get("repo", ""))
            iid = str(mr.get("iid", ""))
            mr_label = f"{repo} !{iid}" if repo and iid else str(mr.get("url", ""))
            mr_url = str(mr.get("url", ""))
            for disc in discussions:
                if not isinstance(disc, dict):
                    continue
                status_key = str(disc.get("status", ""))
                rows.append(
                    DashboardReviewCommentRow(
                        mr_url=mr_url,
                        mr_label=mr_label,
                        status=_DISCUSSION_STATUS_DISPLAY.get(status_key, status_key),
                        detail_text=str(disc.get("detail", ""))[:120],
                        ticket_id=ticket.pk,
                    ),
                )
    return rows


def build_recent_activity(overlay: str | None = None) -> list[RecentActivityRow]:
    qs = TaskAttempt.objects.filter(ended_at__isnull=False).select_related("task__ticket")
    if overlay:
        qs = qs.filter(_task_overlay_q(overlay))
    attempts = qs.order_by("-ended_at")[:_RECENT_ACTIVITY_LIMIT]
    rows: list[RecentActivityRow] = []
    for attempt in attempts:
        result_data = attempt.result if isinstance(attempt.result, dict) else {}
        rows.append(
            RecentActivityRow(
                attempt_id=attempt.pk,
                task_id=attempt.task_id,
                ticket_id=attempt.task.ticket_id,
                phase=attempt.task.phase,
                exit_code=attempt.exit_code,
                result_summary=str(result_data.get("summary", "")),
                error=attempt.error[:200] if attempt.error else "",
                ended_at=attempt.ended_at.isoformat() if attempt.ended_at else "",
                execution_target=attempt.get_execution_target_display(),
                input_tokens=attempt.input_tokens,
                output_tokens=attempt.output_tokens,
                cost_usd=attempt.cost_usd,
            ),
        )
    return rows
