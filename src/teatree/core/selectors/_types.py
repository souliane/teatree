from collections.abc import Mapping
from dataclasses import dataclass
from typing import TypedDict


@dataclass(frozen=True, slots=True)
class AutomationSummary:
    running: int
    completed_24h: int
    succeeded_24h: int
    failed_24h: int
    last_completed_at: str
    total_tokens_24h: int
    total_cost_24h: float


@dataclass(frozen=True, slots=True)
class DashboardSummary:
    in_flight_tickets: int
    active_worktrees: int
    pending_headless_tasks: int
    pending_interactive_tasks: int
    pending_reviews: int = 0


_PIPELINE_DISPLAY: dict[str, tuple[str, str]] = {
    "success": ("\u2705", "bg-green-100 text-green-700"),
    "failed": ("\u274c", "bg-red-100 text-red-700"),
    "running": ("\U0001f504", "bg-yellow-100 text-yellow-700"),
    "pending": ("\u23f3", "bg-yellow-100 text-yellow-700"),
}
_PIPELINE_FALLBACK_CSS = "bg-yellow-100 text-yellow-700"


@dataclass(frozen=True, slots=True)
class DashboardMRRow:
    url: str
    title: str
    repo: str
    iid: str
    branch: str
    draft: bool
    pipeline_status: str | None
    pipeline_url: str | None
    pipeline_icon: str
    pipeline_css: str
    approval_count: int
    approval_required: int
    approved_by: list[str]
    review_requested: bool
    reviewer_names: list[str]
    review_channel: str
    review_permalink: str
    e2e_test_plan_url: str
    is_frontend: bool
    needs_reply_count: int = 0


@dataclass(frozen=True, slots=True)
class DashboardWorktreeRow:
    worktree_id: int
    ticket_id: int
    display_id: str
    repo_path: str
    branch: str
    state: str
    db_name: str


@dataclass(frozen=True, slots=True)
class DashboardTicketRow:
    ticket_id: int
    display_id: str
    issue_url: str
    has_issue: bool
    issue_title: str
    state: str
    tracker_status: str
    notion_status: str
    notion_url: str
    variant: str
    variant_url: str
    repos: list[str]
    ongoing_tasks: int
    total_tasks: int
    labels: list[str]
    mrs: list[DashboardMRRow]
    transitions: list[tuple[str, str]]  # (method_name, label)


@dataclass(frozen=True, slots=True)
class DashboardTaskRow:
    task_id: int
    ticket_id: int
    ticket_display_id: str
    execution_reason: str
    status: str
    claimed_by: str
    last_error: str
    result_summary: str
    session_agent_id: str
    phase: str
    elapsed_time: str = ""
    heartbeat_age: str = ""


@dataclass(frozen=True, slots=True)
class ActiveSessionRow:
    pid: int
    uptime: str
    kind: str  # "headless", "interactive", "ttyd", "manual"
    task_id: int | None
    ticket_id: int | None
    ticket_display_id: str
    phase: str
    launch_url: str
    session_id: str = ""
    cwd: str = ""
    name: str = ""


class DiscussionData(TypedDict, total=False):
    status: str
    detail: str


@dataclass(frozen=True, slots=True)
class ReviewCommentDetail:
    status: str
    detail_text: str


@dataclass(frozen=True, slots=True)
class ActionRequiredItem:
    kind: str  # "interactive_task", "needs_review_request", "needs_reply", "needs_approval"
    label: str
    url: str
    ticket_id: int
    detail: str
    slack_url: str = ""
    review_comments: tuple[ReviewCommentDetail, ...] = ()


@dataclass(frozen=True, slots=True)
class RecentActivityRow:
    attempt_id: int
    task_id: int
    ticket_id: int
    ticket_display_id: str
    issue_url: str
    phase: str
    exit_code: int | None
    result_summary: str
    error: str
    ended_at: str
    execution_target: str
    input_tokens: int | None = None
    output_tokens: int | None = None
    cost_usd: float | None = None


@dataclass(frozen=True, slots=True)
class TaskAttemptDetail:
    attempt_id: int
    started_at: str
    ended_at: str
    exit_code: int | None
    error: str
    result: Mapping[str, object]
    execution_target: str
    agent_session_id: str


@dataclass(frozen=True, slots=True)
class TaskRelatedRow:
    task_id: int
    phase: str
    status: str
    execution_target: str
    execution_reason: str


@dataclass(frozen=True, slots=True)
class TaskDetail:
    task_id: int
    ticket_id: int
    ticket_display_id: str
    phase: str
    status: str
    execution_target: str
    execution_reason: str
    claimed_by: str
    session_agent_id: str
    parent: TaskRelatedRow | None
    children: list[TaskRelatedRow]
    attempts: list[TaskAttemptDetail]


@dataclass(frozen=True, slots=True)
class TaskGraphNode:
    task_id: int
    phase: str
    status: str
    execution_target: str
    execution_reason: str
    depth: int
    children: list["TaskGraphNode"]


@dataclass(frozen=True, slots=True)
class PendingReviewRow:
    url: str
    title: str
    repo: str
    iid: str
    author: str
    draft: bool
    pipeline_status: str
    pipeline_css: str
    pipeline_icon: str
    pipeline_url: str
    updated_at: str


@dataclass(frozen=True, slots=True)
class DashboardSnapshot:
    summary: DashboardSummary
    automation: AutomationSummary
    action_required: list[ActionRequiredItem]
    tickets: list[DashboardTicketRow]
    worktrees: list[DashboardWorktreeRow]
    headless_queue: list[DashboardTaskRow]
    interactive_queue: list[DashboardTaskRow]
    pending_reviews: list[PendingReviewRow]
    active_sessions: list[ActiveSessionRow]
    recent_activity: list[RecentActivityRow]
