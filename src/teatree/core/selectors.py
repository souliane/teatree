import operator
import re
import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from django.db.models import Count, Q, Sum
from django.utils import timezone
from django_fsm import can_proceed

from teatree.core.models import Task, TaskAttempt, Ticket, Worktree

# ── Panel cache ──────────────────────────────────────────────────────

_panel_cache: dict[str, tuple[float, object]] = {}
_DEFAULT_PANEL_TTL = 5.0  # seconds
_SESSIONS_PANEL_TTL = 3.0  # shorter for filesystem I/O heavy panel


def _cached[T](key: str, builder: Callable[[], T], *, ttl: float = _DEFAULT_PANEL_TTL) -> T:
    """Return cached panel result if within TTL, otherwise rebuild."""
    now = time.monotonic()
    entry = _panel_cache.get(key)
    if entry is not None:
        cached_at, value = entry
        if now - cached_at < ttl:
            return value  # type: ignore[return-value]
    value = builder()
    _panel_cache[key] = (now, value)
    return value


def invalidate_panel_cache(panel: str | None = None) -> None:
    """Clear cached panel data. If *panel* is ``None``, clear everything."""
    if panel is None:
        _panel_cache.clear()
    else:
        _panel_cache.pop(panel, None)


_ACTIVE_WORKTREE_STATES = (
    Worktree.State.PROVISIONED,
    Worktree.State.SERVICES_UP,
    Worktree.State.READY,
)


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


@dataclass(frozen=True, slots=True)
class DashboardWorktreeRow:
    worktree_id: int
    ticket_id: int
    display_id: str
    repo_path: str
    branch: str
    state: str
    db_name: str
    ports: dict[str, int]


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
    mrs: list[DashboardMRRow]
    transitions: list[tuple[str, str]]  # (method_name, label)


@dataclass(frozen=True, slots=True)
class DashboardTaskRow:
    task_id: int
    ticket_id: int
    execution_reason: str
    status: str
    claimed_by: str
    last_error: str
    result_summary: str
    session_agent_id: str
    phase: str


@dataclass(frozen=True, slots=True)
class ActiveSessionRow:
    pid: int
    uptime: str
    kind: str  # "headless", "interactive", "ttyd", "manual"
    task_id: int | None
    ticket_id: int | None
    phase: str
    launch_url: str
    session_id: str = ""
    cwd: str = ""
    name: str = ""


@dataclass(frozen=True, slots=True)
class DashboardReviewCommentRow:
    mr_url: str
    mr_label: str
    status: str
    detail_text: str
    ticket_id: int


@dataclass(frozen=True, slots=True)
class ActionRequiredItem:
    kind: str  # "interactive_task", "needs_review_request", "needs_reply", "needs_approval"
    label: str
    url: str
    ticket_id: int
    detail: str


@dataclass(frozen=True, slots=True)
class RecentActivityRow:
    attempt_id: int
    task_id: int
    ticket_id: int
    phase: str
    exit_code: int | None
    result_summary: str
    error: str
    ended_at: str
    execution_target: str


@dataclass(frozen=True, slots=True)
class TaskAttemptDetail:
    attempt_id: int
    started_at: str
    ended_at: str
    exit_code: int | None
    error: str
    result: dict[str, object]
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
class DashboardSnapshot:
    summary: DashboardSummary
    automation: AutomationSummary
    action_required: list[ActionRequiredItem]
    tickets: list[DashboardTicketRow]
    worktrees: list[DashboardWorktreeRow]
    headless_queue: list[DashboardTaskRow]
    interactive_queue: list[DashboardTaskRow]
    active_sessions: list[ActiveSessionRow]
    review_comments: list[DashboardReviewCommentRow]
    recent_activity: list[RecentActivityRow]


def build_task_detail(task_id: int) -> TaskDetail | None:
    task = Task.objects.filter(pk=task_id).select_related("session", "ticket", "parent_task").first()
    if task is None:
        return None

    parent = None
    if task.parent_task_id:
        p = task.parent_task
        parent = TaskRelatedRow(
            task_id=p.pk,
            phase=p.phase,
            status=p.get_status_display(),
            execution_target=p.get_execution_target_display(),
            execution_reason=p.execution_reason[:120],
        )

    children = [
        TaskRelatedRow(
            task_id=c.pk,
            phase=c.phase,
            status=c.get_status_display(),
            execution_target=c.get_execution_target_display(),
            execution_reason=c.execution_reason[:120],
        )
        for c in task.child_tasks.order_by("pk")
    ]

    attempts = [
        TaskAttemptDetail(
            attempt_id=a.pk,
            started_at=a.started_at.isoformat() if a.started_at else "",
            ended_at=a.ended_at.isoformat() if a.ended_at else "",
            exit_code=a.exit_code,
            error=a.error,
            result=a.result if isinstance(a.result, dict) else {},
            execution_target=a.get_execution_target_display(),
            agent_session_id=a.agent_session_id,
        )
        for a in task.attempts.order_by("-pk")
    ]

    return TaskDetail(
        task_id=task.pk,
        ticket_id=task.ticket_id,
        phase=task.phase,
        status=task.get_status_display(),
        execution_target=task.get_execution_target_display(),
        execution_reason=task.execution_reason,
        claimed_by=task.claimed_by,
        session_agent_id=task.session.agent_id if task.session_id else "",
        parent=parent,
        children=children,
        attempts=attempts,
    )


@dataclass(frozen=True, slots=True)
class TaskGraphNode:
    task_id: int
    phase: str
    status: str
    execution_target: str
    execution_reason: str
    depth: int
    children: list["TaskGraphNode"]


def build_task_graph(ticket_id: int) -> list[TaskGraphNode]:
    """Build a tree of tasks for a ticket, rooted at tasks with no parent."""
    tasks = list(Task.objects.filter(ticket_id=ticket_id).select_related("parent_task").order_by("pk"))
    children_map: dict[int | None, list[Task]] = {}
    for task in tasks:
        children_map.setdefault(task.parent_task_id, []).append(task)

    def _build(parent_id: int | None, depth: int) -> list[TaskGraphNode]:
        return [
            TaskGraphNode(
                task_id=task.pk,
                phase=task.phase,
                status=task.get_status_display(),  # ty: ignore[unresolved-attribute]
                execution_target=task.get_execution_target_display(),  # ty: ignore[unresolved-attribute]
                execution_reason=task.execution_reason[:120],
                depth=depth,
                children=_build(task.pk, depth + 1),
            )
            for task in children_map.get(parent_id, [])
        ]

    return _build(None, 0)


_AUTOMATION_WINDOW_HOURS = 24


def _task_overlay_q(overlay: str | None) -> Q:
    """Return a Q filter for task's ticket/session overlay."""
    if not overlay:
        return Q()
    return Q(task__ticket__overlay=overlay) | Q(task__session__overlay=overlay)


def build_automation_summary(overlay: str | None = None) -> AutomationSummary:
    cutoff = timezone.now() - timezone.timedelta(hours=_AUTOMATION_WINDOW_HOURS)
    task_filter = Q(
        execution_target=Task.ExecutionTarget.HEADLESS,
        status=Task.Status.CLAIMED,
    )
    if overlay:
        task_filter &= Q(ticket__overlay=overlay) | Q(session__overlay=overlay)
    running = Task.objects.filter(task_filter).count()
    attempt_filter = Q(
        task__execution_target=Task.ExecutionTarget.HEADLESS,
        ended_at__gte=cutoff,
    ) & _task_overlay_q(overlay)
    recent_attempts = TaskAttempt.objects.filter(attempt_filter)
    completed_24h = recent_attempts.count()
    succeeded_24h = recent_attempts.filter(exit_code=0).count()
    failed_24h = completed_24h - succeeded_24h
    token_stats = recent_attempts.aggregate(
        total_input=Sum("input_tokens"),
        total_output=Sum("output_tokens"),
        total_cost=Sum("cost_usd"),
    )
    total_tokens_24h = (token_stats["total_input"] or 0) + (token_stats["total_output"] or 0)
    total_cost_24h = token_stats["total_cost"] or 0.0
    last_attempt = (
        TaskAttempt.objects.filter(
            Q(task__execution_target=Task.ExecutionTarget.HEADLESS, ended_at__isnull=False) & _task_overlay_q(overlay),
        )
        .order_by("-ended_at")
        .first()
    )
    last_completed_at = last_attempt.ended_at.isoformat() if last_attempt else ""
    return AutomationSummary(
        running=running,
        completed_24h=completed_24h,
        succeeded_24h=succeeded_24h,
        failed_24h=failed_24h,
        last_completed_at=last_completed_at,
        total_tokens_24h=total_tokens_24h,
        total_cost_24h=total_cost_24h,
    )


def build_dashboard_summary(overlay: str | None = None) -> DashboardSummary:
    return DashboardSummary(
        in_flight_tickets=Ticket.objects.in_flight(overlay=overlay).count(),
        active_worktrees=Worktree.objects.active(overlay=overlay).count(),
        pending_headless_tasks=Task.objects.claimable_for_headless(overlay=overlay).count(),
        pending_interactive_tasks=Task.objects.claimable_for_interactive(overlay=overlay).count(),
    )


def build_worktree_rows(overlay: str | None = None) -> list[DashboardWorktreeRow]:
    qs = Worktree.objects.select_related("ticket").exclude(ticket__state=Ticket.State.DELIVERED)
    if overlay:
        qs = qs.filter(overlay=overlay)
    worktrees = qs.order_by("ticket__pk", "pk")
    return [
        DashboardWorktreeRow(
            worktree_id=wt.pk,
            ticket_id=wt.ticket_id,
            display_id=_display_id(wt.ticket),
            repo_path=wt.repo_path,
            branch=wt.branch,
            state=wt.get_state_display(),
            db_name=wt.db_name,
            ports=dict(wt.ports) if isinstance(wt.ports, dict) else {},
        )
        for wt in worktrees
    ]


def build_dashboard_ticket_rows(overlay: str | None = None) -> list[DashboardTicketRow]:
    tickets = (
        Ticket.objects.in_flight(overlay=overlay)
        .annotate(
            ongoing_tasks=Count(
                "tasks",
                filter=Q(tasks__status=Task.Status.CLAIMED),
                distinct=True,
            ),
            total_tasks=Count(
                "tasks",
                filter=~Q(tasks__status__in=(Task.Status.COMPLETED, Task.Status.FAILED)),
                distinct=True,
            ),
        )
        .order_by("pk")
    )
    rows = [
        DashboardTicketRow(
            ticket_id=ticket.pk,
            display_id=_display_id(ticket),
            issue_url=ticket.issue_url,
            has_issue="issues/" in ticket.issue_url or "work_items/" in ticket.issue_url,
            issue_title=_extra_str(ticket, "issue_title") or _first_mr_title(ticket),
            state=ticket.get_state_display(),
            tracker_status=_tracker_status_label(ticket),
            notion_status=_extra_str(ticket, "notion_status"),
            notion_url=_extra_str(ticket, "notion_url"),
            variant=ticket.variant,
            variant_url=_variant_url(ticket.variant),
            repos=list(ticket.repos),
            ongoing_tasks=ticket.ongoing_tasks,
            total_tasks=ticket.total_tasks,
            mrs=_build_mr_rows(ticket),
            transitions=available_ticket_transitions(ticket),
        )
        for ticket in tickets
    ]
    return _sort_ticket_rows(rows)


def _mr_latest_updated_at(row: DashboardTicketRow) -> str:
    """Return the most recent MR updated_at for sorting (empty string if no MR)."""
    extra = Ticket.objects.filter(pk=row.ticket_id).values_list("extra", flat=True).first()
    if not isinstance(extra, dict):
        return ""
    mrs = extra.get("mrs", {})
    if isinstance(mrs, dict):
        timestamps = [str(mr.get("updated_at", "")) for mr in mrs.values() if isinstance(mr, dict)]
        return max(timestamps, default="")
    return str(extra.get("updated_at", ""))


def _sort_ticket_rows(rows: list[DashboardTicketRow]) -> list[DashboardTicketRow]:
    """Sort: tickets with MRs first (by updated_at DESC), then without MR by board position."""
    with_mr: list[tuple[str, DashboardTicketRow]] = []
    without_mr: list[tuple[int, DashboardTicketRow]] = []

    for row in rows:
        if row.mrs:
            with_mr.append((_mr_latest_updated_at(row), row))
        else:
            extra = Ticket.objects.filter(pk=row.ticket_id).values_list("extra", flat=True).first()
            position = int(extra.get("board_position", 9999)) if isinstance(extra, dict) else 9999
            without_mr.append((position, row))

    with_mr.sort(key=operator.itemgetter(0), reverse=True)
    without_mr.sort(key=operator.itemgetter(0))

    return [row for _, row in with_mr] + [row for _, row in without_mr]


def _last_error_for_tasks(task_ids: list[int]) -> dict[int, str]:
    """Return the most recent non-empty error per task from TaskAttempt."""
    from django.db.models import Max  # noqa: PLC0415

    latest_ids = (
        TaskAttempt.objects.filter(task_id__in=task_ids, error__gt="")
        .values("task_id")
        .annotate(latest_pk=Max("pk"))
        .values_list("latest_pk", flat=True)
    )
    attempts = TaskAttempt.objects.filter(pk__in=latest_ids).values_list("task_id", "error")
    return dict(attempts)


_HIDDEN_STATUSES = (Task.Status.COMPLETED, Task.Status.FAILED)


def _last_result_for_tasks(task_ids: list[int]) -> dict[int, str]:
    from django.db.models import Max  # noqa: PLC0415

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
    qs = Task.objects.filter(execution_target=target).select_related("ticket", "session")
    if overlay:
        qs = qs.filter(Q(ticket__overlay=overlay) | Q(session__overlay=overlay))
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
    return [
        DashboardTaskRow(
            task_id=task.pk,
            ticket_id=task.ticket_id,
            execution_reason=task.execution_reason,
            status=task.get_status_display(),
            claimed_by=task.claimed_by,
            last_error=errors.get(task.pk, ""),
            result_summary=results.get(task.pk, ""),
            session_agent_id=task.session.agent_id if task.session_id else "",
            phase=task.phase,
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


def build_action_required(overlay: str | None = None) -> list[ActionRequiredItem]:
    """Aggregate all items that need human attention."""
    task_qs = Task.objects.filter(
        execution_target=Task.ExecutionTarget.INTERACTIVE,
        status=Task.Status.PENDING,
    ).select_related("ticket")
    if overlay:
        task_qs = task_qs.filter(Q(ticket__overlay=overlay) | Q(session__overlay=overlay))
    items: list[ActionRequiredItem] = [
        ActionRequiredItem(
            kind="interactive_task",
            label=f"#{task.ticket.ticket_number} — interactive task",
            url="",
            ticket_id=task.ticket_id,
            detail=task.execution_reason[:120],
        )
        for task in task_qs
    ]

    items.extend(_action_items_from_mrs(overlay))
    return items


def _action_items_from_mrs(overlay: str | None = None) -> list[ActionRequiredItem]:
    """Scan in-flight MRs for review/approval needs."""
    items: list[ActionRequiredItem] = []
    for ticket in Ticket.objects.in_flight(overlay=overlay):
        extra = ticket.extra if isinstance(ticket.extra, dict) else {}
        mrs = extra.get("mrs", {})
        if not isinstance(mrs, dict):
            continue
        for mr in mrs.values():
            items.extend(_check_mr(mr, ticket))
    return items


def _check_mr(mr: dict, ticket: "Ticket") -> list[ActionRequiredItem]:
    """Return action items for a single MR dict."""
    if not isinstance(mr, dict) or mr.get("draft"):
        return []
    repo = str(mr.get("repo", ""))
    iid = str(mr.get("iid", ""))
    mr_url = str(mr.get("url", ""))
    mr_label = f"{repo} !{iid}"
    pipeline = mr.get("pipeline_status")
    approvals = mr.get("approvals", {})
    if not isinstance(approvals, dict):
        approvals = {}
    count = int(approvals.get("count", 0))
    required = int(approvals.get("required", 1))
    items: list[ActionRequiredItem] = []

    if pipeline == "success" and not mr.get("review_permalink") and not mr.get("review_requested"):
        items.append(
            ActionRequiredItem(
                kind="needs_review_request",
                label=f"{mr_label} — ready for review request",
                url=mr_url,
                ticket_id=ticket.pk,
                detail="CI green, no review posted yet",
            ),
        )

    discussions = mr.get("discussions", [])
    if isinstance(discussions, list):
        needs_reply = sum(1 for d in discussions if isinstance(d, dict) and d.get("status") == "needs_reply")
        if needs_reply:
            items.append(
                ActionRequiredItem(
                    kind="needs_reply",
                    label=f"{mr_label} — {needs_reply} comment{'s' if needs_reply > 1 else ''} need reply",
                    url=mr_url,
                    ticket_id=ticket.pk,
                    detail="Review threads waiting for your response",
                ),
            )

    if pipeline == "success" and mr.get("review_requested") and count < required:
        items.append(
            ActionRequiredItem(
                kind="needs_approval",
                label=f"{mr_label} — waiting for approval ({count}/{required})",
                url=mr_url,
                ticket_id=ticket.pk,
                detail="Review requested, approval pending",
            ),
        )

    return items


_CLAUDE_SESSIONS_DIR = Path.home() / ".claude" / "sessions"
_MINUTES_PER_HOUR = 60
_MS_PER_MINUTE = 60_000


def _uptime_from_epoch_ms(started_at_ms: int) -> str:
    """Convert epoch milliseconds to a human-readable uptime string."""
    elapsed = int(timezone.now().timestamp() * 1000) - started_at_ms
    minutes = max(0, elapsed // _MS_PER_MINUTE)
    if minutes < _MINUTES_PER_HOUR:
        return f"{minutes}m"
    hours = minutes // _MINUTES_PER_HOUR
    remaining = minutes % _MINUTES_PER_HOUR
    return f"{hours}h{remaining:02d}m"


def build_active_sessions() -> list[ActiveSessionRow]:
    """Discover active claude sessions from ~/.claude/sessions/ files."""
    import json  # noqa: PLC0415
    import os  # noqa: PLC0415

    if not _CLAUDE_SESSIONS_DIR.is_dir():
        return []

    claimed_tasks = {t.pk: t for t in Task.objects.filter(status=Task.Status.CLAIMED).select_related("ticket")}

    # Match tasks to sessions by agent_session_id
    session_to_task: dict[str, Task] = {}
    for task in claimed_tasks.values():
        last_attempt = task.attempts.order_by("-pk").first()
        if last_attempt and last_attempt.agent_session_id:
            session_to_task[last_attempt.agent_session_id] = task

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
        started_at = data.get("startedAt", 0)
        cwd = str(data.get("cwd", ""))
        name = str(data.get("name", ""))

        # Match to a task via session ID
        task = session_to_task.get(session_id)
        task_id = task.pk if task else None
        ticket_id = task.ticket.pk if task else None
        phase = task.phase if task else ""
        kind = "headless" if task and task.execution_target == Task.ExecutionTarget.HEADLESS else "interactive"

        sessions.append(
            ActiveSessionRow(
                pid=pid,
                uptime=_uptime_from_epoch_ms(started_at) if started_at else "",
                kind=kind,
                task_id=task_id,
                ticket_id=ticket_id,
                phase=phase,
                launch_url="",
                session_id=session_id,
                cwd=cwd,
                name=name,
            ),
        )

    return sessions


def build_dashboard_snapshot(overlay: str | None = None) -> DashboardSnapshot:
    sfx = f":{overlay}" if overlay else ""
    return DashboardSnapshot(
        summary=_cached(f"summary{sfx}", lambda: build_dashboard_summary(overlay)),
        automation=_cached(f"automation{sfx}", lambda: build_automation_summary(overlay)),
        action_required=_cached(f"action_required{sfx}", lambda: build_action_required(overlay)),
        tickets=_cached(f"tickets{sfx}", lambda: build_dashboard_ticket_rows(overlay)),
        worktrees=_cached(f"worktrees{sfx}", lambda: build_worktree_rows(overlay)),
        headless_queue=_cached(f"headless_queue{sfx}", lambda: build_headless_queue(overlay=overlay)),
        interactive_queue=_cached(f"queue{sfx}", lambda: build_interactive_queue(pending_only=True, overlay=overlay)),
        active_sessions=_cached("sessions", build_active_sessions, ttl=_SESSIONS_PANEL_TTL),
        review_comments=_cached(f"review_comments{sfx}", lambda: build_review_comments(overlay)),
        recent_activity=_cached(f"activity{sfx}", lambda: build_recent_activity(overlay)),
    )


def _display_id(ticket: Ticket) -> str:
    return ticket.ticket_number


_TICKET_TRANSITIONS = [
    ("scope", "Scope"),
    ("start", "Start"),
    ("code", "Code"),
    ("test", "Test"),
    ("review", "Review"),
    ("ship", "Ship"),
    ("request_review", "Request review"),
    ("mark_merged", "Mark merged"),
    ("mark_delivered", "Mark delivered"),
    ("rework", "Rework"),
]


def available_ticket_transitions(ticket: Ticket) -> list[tuple[str, str]]:
    """Return (method_name, label) pairs for transitions available from the current state."""
    return [(name, label) for name, label in _TICKET_TRANSITIONS if can_proceed(getattr(ticket, name))]


def _extra_str(ticket: Ticket, key: str) -> str:
    extra = ticket.extra if isinstance(ticket.extra, dict) else {}
    return str(extra.get(key, ""))


def _variant_url(variant: str) -> str:
    if not variant:
        return ""
    from django.core.exceptions import ImproperlyConfigured  # noqa: PLC0415

    from teatree.core.overlay_loader import get_overlay  # noqa: PLC0415

    try:
        url_template = get_overlay().config.get_dev_env_url()
    except ImproperlyConfigured:
        return ""
    if not url_template:
        return ""
    try:
        return url_template.format(variant=variant.lower())
    except (KeyError, ValueError):
        return ""


def _first_mr_title(ticket: Ticket) -> str:
    extra = ticket.extra if isinstance(ticket.extra, dict) else {}
    mrs = extra.get("mrs", {})
    if isinstance(mrs, dict):
        for mr in mrs.values():
            if isinstance(mr, dict):
                title = str(mr.get("title", ""))
                if title:
                    return title
    return ""


def _tracker_status_label(ticket: Ticket) -> str:
    raw = _extra_str(ticket, "tracker_status")
    if not raw:
        return ""
    return raw.replace("Process::", "").replace("Process:: ", "").strip()


def _build_mr_rows(ticket: Ticket) -> list[DashboardMRRow]:
    from teatree.core.overlay_loader import get_overlay  # noqa: PLC0415

    frontend_repos = get_overlay().config.get_frontend_repos()
    extra = ticket.extra if isinstance(ticket.extra, dict) else {}
    mrs_data = extra.get("mrs", {})
    if not isinstance(mrs_data, dict):
        return []
    rows = []
    for mr in mrs_data.values():
        if not isinstance(mr, dict):
            continue
        approvals = mr.get("approvals", {})
        if not isinstance(approvals, dict):
            approvals = {}
        url = str(mr.get("url", ""))
        iid = str(mr.get("iid", ""))
        if not iid:
            iid_match = re.search(r"/merge_requests/(\d+)", url)
            iid = iid_match.group(1) if iid_match else ""
        rows.append(
            DashboardMRRow(
                url=url,
                title=str(mr.get("title", "")),
                repo=str(mr.get("repo", "")),
                iid=iid,
                branch=str(mr.get("branch", "")),
                draft=bool(mr.get("draft")),
                pipeline_status=mr.get("pipeline_status"),
                pipeline_url=mr.get("pipeline_url"),
                pipeline_icon=_PIPELINE_DISPLAY.get(mr.get("pipeline_status", ""), ("", ""))[0]
                or str(mr.get("pipeline_status", "")),
                pipeline_css=_PIPELINE_DISPLAY.get(mr.get("pipeline_status", ""), ("", _PIPELINE_FALLBACK_CSS))[1],
                approval_count=int(approvals.get("count", 0)),
                approval_required=int(approvals.get("required", 1)),
                approved_by=_list_of_str(approvals.get("approved_by", [])),
                review_requested=bool(mr.get("review_requested")),
                reviewer_names=_list_of_str(mr.get("reviewer_names", [])),
                review_channel=str(mr.get("review_channel", "")),
                review_permalink=str(mr.get("review_permalink", "")),
                e2e_test_plan_url=str(mr.get("e2e_test_plan_url", "")),
                is_frontend=str(mr.get("repo", "")) in frontend_repos,
            ),
        )
    return rows


def _list_of_str(value: object) -> list[str]:
    if isinstance(value, list):
        return [str(v) for v in value]
    return []


_DISCUSSION_STATUS_DISPLAY = {
    "waiting_reviewer": "Waiting reviewer",
    "needs_reply": "Needs reply",
    "addressed": "Addressed",
}


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


_RECENT_ACTIVITY_LIMIT = 10


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
            ),
        )
    return rows
