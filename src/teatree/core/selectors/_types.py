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
    execution_target: str
    phase: str
    issue_url: str = ""
    elapsed_time: str = ""
    heartbeat_age: str = ""


@dataclass(frozen=True, slots=True)
class ActiveSessionRow:
    pid: int
    uptime: str
    kind: str  # "headless", "interactive", "manual"
    task_id: int | None
    ticket_id: int | None
    ticket_display_id: str
    phase: str
    launch_url: str
    issue_url: str = ""
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
    kind: str  # "interactive_task", "needs_review_request", "needs_reply", "needs_approval", "review_draft"
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
class UnifiedSessionRow:
    """A single row in the unified Sessions panel."""

    row_status: str  # "running", "queued", "completed", "failed", "manual"
    execution_target: str  # "headless", "interactive", "manual"
    task_id: int | None
    ticket_id: int | None
    ticket_display_id: str
    issue_url: str
    phase: str
    execution_reason: str
    result_summary: str
    error: str
    # Timing
    queued_at: str
    started_at: str
    stopped_at: str
    elapsed_time: str
    heartbeat_age: str
    # Process info (running sessions)
    pid: int | None = None
    session_id: str = ""
    cwd: str = ""
    launch_url: str = ""
    # Task action context
    claimed_by: str = ""
    # Activity details
    exit_code: int | None = None
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
