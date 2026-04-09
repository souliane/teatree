import importlib
from collections.abc import Callable, Mapping
from pathlib import Path

from django.http import Http404, HttpRequest, HttpResponse
from django.shortcuts import get_object_or_404
from django.template.response import TemplateResponse
from django.templatetags.static import static
from django.views import View

from teatree.core.overlay_loader import get_all_overlay_names, get_all_overlays
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
    build_task_detail,
    build_task_graph,
    build_worktree_rows,
)
from teatree.utils import git as git_utils

_PANEL_TEMPLATES = {
    "summary": "teatree/partials/dashboard_summary.html",
    "automation": "teatree/partials/dashboard_automation.html",
    "action_required": "teatree/partials/dashboard_action_required.html",
    "tickets": "teatree/partials/dashboard_tickets.html",
    "worktrees": "teatree/partials/dashboard_worktrees.html",
    "headless_queue": "teatree/partials/dashboard_headless_queue.html",
    "queue": "teatree/partials/dashboard_queue.html",
    "sessions": "teatree/partials/dashboard_sessions.html",
    "activity": "teatree/partials/dashboard_activity.html",
}


def _extract_overlay(request: HttpRequest) -> str | None:
    """Extract overlay name from ``?overlay=`` query parameter."""
    overlay = request.GET.get("overlay", "").strip()
    return overlay or None


class DashboardView(View):
    def get(self, request: HttpRequest) -> HttpResponse:
        import json  # noqa: PLC0415

        overlay = _extract_overlay(request)
        try:
            git_sha = git_utils.run(args=["rev-parse", "--short", "HEAD"])
            git_branch = git_utils.current_branch()
        except Exception:  # noqa: BLE001
            git_sha, git_branch = "", ""
        all_overlays = get_all_overlays()
        overlay_branches = _build_overlay_branches(all_overlays)
        overlays = get_all_overlay_names()
        teatree_logo = static("teatree/img/teatree-logo.jpg")
        overlay_logos = {name: ov.config.dashboard_logo or teatree_logo for name, ov in all_overlays.items()}
        if overlay and overlay in all_overlays:
            logo_url = all_overlays[overlay].config.dashboard_logo or teatree_logo
        else:
            logo_url = teatree_logo
        from teatree.agents.services import get_terminal_mode  # noqa: PLC0415
        from teatree.agents.terminal_launcher import detect_available_apps  # noqa: PLC0415
        from teatree.config import load_config  # noqa: PLC0415

        return TemplateResponse(
            request,
            "teatree/dashboard.html",
            {
                "snapshot": build_dashboard_snapshot(overlay=overlay),
                "logo_url": logo_url,
                "overlays": overlays,
                "selected_overlay": overlay or "",
                "terminal_mode": get_terminal_mode(),
                "sync_pending": True,
                "overlay_logos_json": json.dumps(overlay_logos),
                "default_logo": teatree_logo,
                "git_sha": git_sha,
                "git_branch": git_branch,
                "overlay_branches": overlay_branches,
                "terminal_apps": detect_available_apps(),
                "contribute_mode": load_config().user.contribute,
            },
        )


class DashboardPanelView(View):
    def get(self, request: HttpRequest, panel: str) -> HttpResponse:
        if not getattr(request, "htmx", False):
            raise Http404
        template_name = _PANEL_TEMPLATES.get(panel)
        if template_name is None:
            raise Http404
        overlay = _extract_overlay(request)
        show_dismissed = request.GET.get("show_dismissed") == "1"
        return TemplateResponse(
            request,
            template_name,
            _panel_context(panel, show_dismissed=show_dismissed, overlay=overlay),
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


class TaskGraphView(View):
    def get(self, request: HttpRequest, ticket_id: int) -> HttpResponse:
        from teatree.core.models import Ticket  # noqa: PLC0415

        ticket = get_object_or_404(Ticket, pk=ticket_id)
        graph = build_task_graph(ticket_id)
        return TemplateResponse(
            request,
            "teatree/partials/task_graph.html",
            {"ticket_id": ticket.ticket_number, "graph": graph},
        )


class TicketLifecycleView(View):
    def get(self, request: HttpRequest, ticket_id: int) -> HttpResponse:
        from teatree.core.models import Ticket  # noqa: PLC0415
        from teatree.core.selectors import build_ticket_lifecycle_mermaid  # noqa: PLC0415

        ticket = get_object_or_404(Ticket, pk=ticket_id)
        mermaid = build_ticket_lifecycle_mermaid(ticket_id)
        return TemplateResponse(
            request,
            "teatree/partials/ticket_lifecycle.html",
            {"ticket_id": ticket.ticket_number, "mermaid": mermaid},
        )


type _PanelBuilder = Callable[[bool, str | None], dict[str, object]]

_PANEL_BUILDERS: dict[str, _PanelBuilder] = {
    "summary": lambda _d, o: {"summary": build_dashboard_summary(overlay=o)},
    "automation": lambda _d, o: {"automation": build_automation_summary(overlay=o)},
    "action_required": lambda _d, o: {"action_items": build_action_required(overlay=o)},
    "tickets": lambda _d, o: {"tickets": build_dashboard_ticket_rows(overlay=o)},
    "worktrees": lambda _d, o: {"worktrees": build_worktree_rows(overlay=o)},
    "headless_queue": lambda d, o: {
        "headless_queue": build_headless_queue(include_dismissed=d, overlay=o),
        "show_dismissed": d,
    },
    "queue": lambda d, o: {
        "queue": build_interactive_queue(include_dismissed=d, overlay=o),
        "show_dismissed": d,
    },
    "sessions": lambda _d, _o: {"sessions": build_active_sessions()},
    "activity": lambda _d, o: {"activity": build_recent_activity(overlay=o)},
}


def _build_overlay_branches(all_overlays: Mapping[str, object]) -> dict[str, str]:
    """Build overlay name -> git branch mapping for all overlays."""
    from teatree.config import discover_overlays  # noqa: PLC0415

    branches: dict[str, str] = {}
    for entry in discover_overlays():
        repo_path = None
        if entry.project_path:
            repo_path = Path(entry.project_path)
        elif entry.name in all_overlays:
            ov = all_overlays[entry.name]
            mod = importlib.import_module(type(ov).__module__)
            if hasattr(mod, "__file__") and mod.__file__:
                repo_path = Path(mod.__file__).resolve().parent
        if repo_path:
            try:
                branch = git_utils.current_branch(repo=str(repo_path))
            except OSError:
                branch = ""
            branches[entry.name] = branch or "unknown"
        else:
            branches[entry.name] = "unknown"
    return branches


def _panel_context(panel: str, *, show_dismissed: bool = False, overlay: str | None = None) -> dict[str, object]:
    builder = _PANEL_BUILDERS.get(panel)
    if builder is None:
        msg = f"Unsupported panel: {panel}"
        raise ValueError(msg)
    return builder(show_dismissed, overlay)
