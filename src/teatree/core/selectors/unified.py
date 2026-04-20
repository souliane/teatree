"""Unified sessions selector — merges queues, active sessions, and activity."""

from teatree.core.selectors._types import UnifiedSessionRow

from .activity import build_active_sessions, build_recent_activity
from .queues import build_headless_queue, build_interactive_queue


def build_unified_sessions(
    *,
    overlay: str | None = None,
    include_dismissed: bool = False,
) -> list[UnifiedSessionRow]:
    """Build a single list of all session/task rows for the unified panel."""
    rows: list[UnifiedSessionRow] = []
    seen_task_ids: set[int] = set()

    # 1. Active sessions (running processes)
    for s in build_active_sessions():
        if s.task_id:
            seen_task_ids.add(s.task_id)
        rows.append(
            UnifiedSessionRow(
                row_status="running",
                execution_target=s.kind,
                task_id=s.task_id,
                ticket_id=s.ticket_id,
                ticket_display_id=s.ticket_display_id,
                issue_url=s.issue_url,
                phase=s.phase,
                execution_reason="",
                result_summary="",
                error="",
                queued_at="",
                started_at=s.uptime,
                stopped_at="",
                elapsed_time=s.uptime,
                heartbeat_age="",
                pid=s.pid,
                session_id=s.session_id,
                cwd=s.cwd,
                launch_url=s.launch_url,
            ),
        )

    # 2. Queued/claimed tasks (headless + interactive)
    for task_row in [
        *build_headless_queue(include_dismissed=include_dismissed, overlay=overlay),
        *build_interactive_queue(include_dismissed=include_dismissed, overlay=overlay),
    ]:
        if task_row.task_id in seen_task_ids:
            continue
        seen_task_ids.add(task_row.task_id)
        status = task_row.status.lower()
        row_status = "running" if status == "claimed" else "queued"
        target = "headless" if "headless" in task_row.session_agent_id.lower() else "interactive"
        rows.append(
            UnifiedSessionRow(
                row_status=row_status,
                execution_target=target,
                task_id=task_row.task_id,
                ticket_id=task_row.ticket_id,
                ticket_display_id=task_row.ticket_display_id,
                issue_url=task_row.issue_url,
                phase=task_row.phase,
                execution_reason=task_row.execution_reason,
                result_summary=task_row.result_summary,
                error=task_row.last_error,
                queued_at="",
                started_at="",
                stopped_at="",
                elapsed_time=task_row.elapsed_time,
                heartbeat_age=task_row.heartbeat_age,
                claimed_by=task_row.claimed_by,
            ),
        )

    # 3. Recent activity (completed/failed)
    for act in build_recent_activity(overlay=overlay):
        if act.task_id in seen_task_ids:
            continue
        seen_task_ids.add(act.task_id)
        row_status = "completed" if act.exit_code == 0 else "failed"
        rows.append(
            UnifiedSessionRow(
                row_status=row_status,
                execution_target=act.execution_target,
                task_id=act.task_id,
                ticket_id=act.ticket_id,
                ticket_display_id=act.ticket_display_id,
                issue_url=act.issue_url,
                phase=act.phase,
                execution_reason="",
                result_summary=act.result_summary,
                error=act.error,
                queued_at="",
                started_at="",
                stopped_at=act.ended_at,
                elapsed_time="",
                heartbeat_age="",
                exit_code=act.exit_code,
                input_tokens=act.input_tokens,
                output_tokens=act.output_tokens,
                cost_usd=act.cost_usd,
            ),
        )

    return rows
