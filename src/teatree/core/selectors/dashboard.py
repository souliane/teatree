import operator
import re

from django.core.exceptions import ImproperlyConfigured
from django.db.models import Count, Q
from django_fsm import can_proceed

from teatree.core.models import Task, Ticket, Worktree
from teatree.core.overlay_loader import get_overlay

from ._cache import _SESSIONS_PANEL_TTL, _cached
from ._helpers import _display_id, _extra_str, _list_of_str
from ._types import (
    _PIPELINE_DISPLAY,
    _PIPELINE_FALLBACK_CSS,
    DashboardMRRow,
    DashboardSnapshot,
    DashboardSummary,
    DashboardTicketRow,
    DashboardWorktreeRow,
)
from .activity import build_active_sessions, build_recent_activity
from .automation import build_action_required, build_automation_summary
from .queues import build_headless_queue, build_interactive_queue

_ACTIVE_WORKTREE_STATES = (
    Worktree.State.PROVISIONED,
    Worktree.State.SERVICES_UP,
    Worktree.State.READY,
)

_TICKET_TRANSITIONS = [
    ("scope", "Scope"),
    ("start", "Start"),
    ("code", "Code"),
    ("test", "Test"),
    ("review", "Review"),
    ("ship", "Ship"),
    ("request_review", "Request review"),
    ("mark_merged", "Merge"),
    ("mark_delivered", "Mark delivered"),
    ("rework", "Rework"),
]


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
        qs = qs.filter(Q(overlay=overlay) | Q(overlay=""))
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
    rows = []
    for ticket in tickets:
        mrs = _build_mr_rows(ticket)
        title = _extra_str(ticket, "issue_title") or _first_mr_title(ticket)
        has_issue = "issues/" in ticket.issue_url or "work_items/" in ticket.issue_url
        display_id = _display_id(ticket)
        # Skip tickets with nothing visible in the row: no issue link to display,
        # no title, and no MR data.
        if not has_issue and not mrs and not title:
            continue
        extra = ticket.extra if isinstance(ticket.extra, dict) else {}
        raw_labels = _list_of_str(extra.get("labels", []))
        variant_lower = ticket.variant.lower()
        display_labels = [lbl for lbl in raw_labels if not lbl.startswith("Process::") and lbl.lower() != variant_lower]
        rows.append(
            DashboardTicketRow(
                ticket_id=ticket.pk,
                display_id=display_id,
                issue_url=ticket.issue_url,
                has_issue=has_issue,
                issue_title=title,
                state=ticket.get_state_display(),
                tracker_status=_tracker_status_label(ticket),
                notion_status=_extra_str(ticket, "notion_status"),
                notion_url=_extra_str(ticket, "notion_url"),
                variant=ticket.variant,
                variant_url=_variant_url(ticket.variant),
                repos=list(ticket.repos),
                ongoing_tasks=ticket.ongoing_tasks,
                total_tasks=ticket.total_tasks,
                labels=display_labels,
                mrs=mrs,
                transitions=available_ticket_transitions(ticket),
            ),
        )
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
        recent_activity=_cached(f"activity{sfx}", lambda: build_recent_activity(overlay)),
    )


def available_ticket_transitions(ticket: Ticket) -> list[tuple[str, str]]:
    """Return (method_name, label) pairs for transitions available from the current state."""
    return [(name, label) for name, label in _TICKET_TRANSITIONS if can_proceed(getattr(ticket, name))]


def _variant_url(variant: str) -> str:
    if not variant:
        return ""
    try:
        url_template = get_overlay().config.dev_env_url
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
    frontend_repos = get_overlay().config.frontend_repos
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
