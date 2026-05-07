"""Selectors package — read-only queries for tickets, sessions, tasks, and worktrees.

Loop scanners and the CLI consume these selectors to render the statusline
and answer status queries without bypassing the FSM models.
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
    DashboardTaskRow,
    DiscussionData,
    RecentActivityRow,
    ReviewCommentDetail,
    TaskAttemptDetail,
    TaskDetail,
    TaskGraphNode,
    TaskRelatedRow,
    UnifiedSessionRow,
)
from .activity import build_active_sessions, build_recent_activity
from .automation import _check_pr, build_action_required, build_automation_summary
from .queues import (
    _last_result_for_tasks,
    build_headless_queue,
    build_interactive_queue,
)
from .tasks import build_task_detail, build_task_graph, build_ticket_lifecycle_mermaid
from .unified import build_unified_sessions

__all__ = [
    "_CLAUDE_SESSIONS_DIR",
    "ActionRequiredItem",
    "ActiveSessionRow",
    "AutomationSummary",
    "DashboardTaskRow",
    "DiscussionData",
    "RecentActivityRow",
    "ReviewCommentDetail",
    "TaskAttemptDetail",
    "TaskDetail",
    "TaskGraphNode",
    "TaskRelatedRow",
    "UnifiedSessionRow",
    "_cached",
    "_check_pr",
    "_display_id",
    "_extra_str",
    "_humanize_duration",
    "_last_result_for_tasks",
    "_list_of_str",
    "_overlay_q",
    "_panel_cache",
    "_task_overlay_q",
    "_uptime_from_epoch_ms",
    "build_action_required",
    "build_active_sessions",
    "build_automation_summary",
    "build_headless_queue",
    "build_interactive_queue",
    "build_recent_activity",
    "build_task_detail",
    "build_task_graph",
    "build_ticket_lifecycle_mermaid",
    "build_unified_sessions",
    "invalidate_panel_cache",
]
