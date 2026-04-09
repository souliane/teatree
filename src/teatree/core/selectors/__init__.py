"""Selectors package — query functions for dashboard panels and task details.

All public names are re-exported here for backward compatibility so that
``from teatree.core.selectors import ...`` continues to work unchanged.
"""

from ._cache import _cached, _panel_cache, invalidate_panel_cache
from ._filters import _overlay_q, _task_overlay_q
from ._helpers import (
    _CLAUDE_SESSIONS_DIR,
    _display_id,
    _extra_str,
    _humanize_duration,
    _list_of_str,
    _uptime_from_epoch_ms,
)
from ._types import (
    ActionRequiredItem,
    ActiveSessionRow,
    AutomationSummary,
    DashboardMRRow,
    DashboardSnapshot,
    DashboardSummary,
    DashboardTaskRow,
    DashboardTicketRow,
    DashboardWorktreeRow,
    DiscussionData,
    RecentActivityRow,
    ReviewCommentDetail,
    TaskAttemptDetail,
    TaskDetail,
    TaskGraphNode,
    TaskRelatedRow,
)
from .activity import build_active_sessions, build_recent_activity
from .automation import _check_mr, build_action_required, build_automation_summary
from .dashboard import (
    _build_mr_rows,
    _first_mr_title,
    _variant_url,
    available_ticket_transitions,
    build_dashboard_snapshot,
    build_dashboard_summary,
    build_dashboard_ticket_rows,
    build_worktree_rows,
)
from .queues import (
    _last_result_for_tasks,
    build_headless_queue,
    build_interactive_queue,
)
from .tasks import build_task_detail, build_task_graph, build_ticket_lifecycle_mermaid

__all__ = [
    "_CLAUDE_SESSIONS_DIR",
    "ActionRequiredItem",
    "ActiveSessionRow",
    "AutomationSummary",
    "DashboardMRRow",
    "DashboardSnapshot",
    "DashboardSummary",
    "DashboardTaskRow",
    "DashboardTicketRow",
    "DashboardWorktreeRow",
    "DiscussionData",
    "RecentActivityRow",
    "ReviewCommentDetail",
    "TaskAttemptDetail",
    "TaskDetail",
    "TaskGraphNode",
    "TaskRelatedRow",
    "_build_mr_rows",
    "_cached",
    "_check_mr",
    "_display_id",
    "_extra_str",
    "_first_mr_title",
    "_humanize_duration",
    "_last_result_for_tasks",
    "_list_of_str",
    "_overlay_q",
    "_panel_cache",
    "_task_overlay_q",
    "_uptime_from_epoch_ms",
    "_variant_url",
    "available_ticket_transitions",
    "build_action_required",
    "build_active_sessions",
    "build_automation_summary",
    "build_dashboard_snapshot",
    "build_dashboard_summary",
    "build_dashboard_ticket_rows",
    "build_headless_queue",
    "build_interactive_queue",
    "build_recent_activity",
    "build_task_detail",
    "build_task_graph",
    "build_ticket_lifecycle_mermaid",
    "build_worktree_rows",
    "invalidate_panel_cache",
]
