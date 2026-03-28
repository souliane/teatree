from django.conf import settings
from django.http import Http404, HttpRequest, HttpResponse
from django.template.response import TemplateResponse
from django.templatetags.static import static
from django.views import View

from teetree.core.selectors import (
    build_action_required,
    build_active_sessions,
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

_PANEL_TEMPLATES = {
    "summary": "teetree/partials/dashboard_summary.html",
    "action_required": "teetree/partials/dashboard_action_required.html",
    "tickets": "teetree/partials/dashboard_tickets.html",
    "worktrees": "teetree/partials/dashboard_worktrees.html",
    "headless_queue": "teetree/partials/dashboard_headless_queue.html",
    "queue": "teetree/partials/dashboard_queue.html",
    "sessions": "teetree/partials/dashboard_sessions.html",
    "review_comments": "teetree/partials/dashboard_review_comments.html",
    "activity": "teetree/partials/dashboard_activity.html",
}


class DashboardView(View):
    _synced = False

    def get(self, request: HttpRequest) -> HttpResponse:
        # Serve immediately with cached data — hx-post sync on page load
        # handles the background refresh, so we don't block the initial render.
        logo_url = getattr(settings, "TEATREE_DASHBOARD_LOGO", None) or static("teetree/img/teatree-logo.svg")
        return TemplateResponse(
            request,
            "teetree/dashboard.html",
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
            "teetree/partials/task_detail_popup.html",
            {"detail": detail},
        )


def _panel_context(panel: str, *, show_dismissed: bool = False) -> dict[str, object]:  # noqa: PLR0911
    if panel == "summary":
        return {"summary": build_dashboard_summary()}
    if panel == "action_required":
        return {"action_items": build_action_required()}
    if panel == "tickets":
        return {"tickets": build_dashboard_ticket_rows()}
    if panel == "worktrees":
        return {"worktrees": build_worktree_rows()}
    if panel == "headless_queue":
        return {
            "headless_queue": build_headless_queue(include_dismissed=show_dismissed),
            "show_dismissed": show_dismissed,
        }
    if panel == "queue":
        return {"queue": build_interactive_queue(include_dismissed=show_dismissed), "show_dismissed": show_dismissed}
    if panel == "sessions":
        return {"sessions": build_active_sessions()}
    if panel == "review_comments":
        return {"review_comments": build_review_comments()}
    if panel == "activity":
        return {"activity": build_recent_activity()}
    msg = f"Unsupported panel: {panel}"
    raise ValueError(msg)
