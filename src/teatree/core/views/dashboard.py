from collections.abc import Callable

from django.http import Http404, HttpRequest, HttpResponse
from django.template.response import TemplateResponse
from django.templatetags.static import static
from django.views import View

from teatree.core.overlay_loader import get_overlay
from teatree.core.selectors import (
    build_action_required,
    build_active_sessions,
    build_automation_summary,
    build_dashboard_snapshot,
    build_dashboard_summary,
    build_dashboard_ticket_rows,
    build_headless_queue,
    build_interactive_queue,
    build_recent_activity,
    build_review_comments,
    build_task_detail,
    build_worktree_rows,
)
from teatree.core.views._startup import perform_sync

_PANEL_TEMPLATES = {
    "summary": "teatree/partials/dashboard_summary.html",
    "automation": "teatree/partials/dashboard_automation.html",
    "action_required": "teatree/partials/dashboard_action_required.html",
    "tickets": "teatree/partials/dashboard_tickets.html",
    "worktrees": "teatree/partials/dashboard_worktrees.html",
    "headless_queue": "teatree/partials/dashboard_headless_queue.html",
    "queue": "teatree/partials/dashboard_queue.html",
    "sessions": "teatree/partials/dashboard_sessions.html",
    "review_comments": "teatree/partials/dashboard_review_comments.html",
    "activity": "teatree/partials/dashboard_activity.html",
}


class DashboardView(View):
    _synced = False

    def get(self, request: HttpRequest) -> HttpResponse:
        if not DashboardView._synced:  # pragma: no branch
            perform_sync()
            DashboardView._synced = True
        logo_url = get_overlay().config.get_dashboard_logo() or static("teatree/img/teatree-logo.svg")
        return TemplateResponse(
            request,
            "teatree/dashboard.html",
            {"snapshot": build_dashboard_snapshot(), "logo_url": logo_url},
        )


class DashboardPanelView(View):
    def get(self, request: HttpRequest, panel: str) -> HttpResponse:
        if not getattr(request, "htmx", False):
            raise Http404
        template_name = _PANEL_TEMPLATES.get(panel)
        if template_name is None:
            raise Http404
        show_dismissed = request.GET.get("show_dismissed") == "1"
        return TemplateResponse(
            request,
            template_name,
            _panel_context(panel, show_dismissed=show_dismissed),
        )


class TaskDetailView(View):
    def get(self, request: HttpRequest, task_id: int) -> HttpResponse:
        detail = build_task_detail(task_id)
        if detail is None:
            raise Http404
        return TemplateResponse(
            request,
            "teatree/partials/task_detail_popup.html",
            {"detail": detail},
        )


type _PanelBuilder = Callable[[bool], dict[str, object]]

_PANEL_BUILDERS: dict[str, _PanelBuilder] = {
    "summary": lambda _d: {"summary": build_dashboard_summary()},
    "automation": lambda _d: {"automation": build_automation_summary()},
    "action_required": lambda _d: {"action_items": build_action_required()},
    "tickets": lambda _d: {"tickets": build_dashboard_ticket_rows()},
    "worktrees": lambda _d: {"worktrees": build_worktree_rows()},
    "headless_queue": lambda d: {"headless_queue": build_headless_queue(include_dismissed=d), "show_dismissed": d},
    "queue": lambda d: {"queue": build_interactive_queue(include_dismissed=d), "show_dismissed": d},
    "sessions": lambda _d: {"sessions": build_active_sessions()},
    "review_comments": lambda _d: {"review_comments": build_review_comments()},
    "activity": lambda _d: {"activity": build_recent_activity()},
}


def _panel_context(panel: str, *, show_dismissed: bool = False) -> dict[str, object]:
    builder = _PANEL_BUILDERS.get(panel)
    if builder is None:
        msg = f"Unsupported panel: {panel}"
        raise ValueError(msg)
    return builder(show_dismissed)
