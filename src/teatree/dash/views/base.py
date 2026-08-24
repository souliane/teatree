"""Shared nav context, request helpers and cross-page write policy for the dash views (#3162)."""

import socket
from typing import TYPE_CHECKING, TypedDict

from django.shortcuts import render

from teatree.config import get_effective_settings

if TYPE_CHECKING:
    from django.http import HttpRequest, HttpResponse

# The top-level pages, in nav order: (url-name, label).
NAV_ITEMS: tuple[tuple[str, str], ...] = (
    ("dash:board", "Board"),
    ("dash:live", "Live"),
    ("dash:cycle_time", "Cycle time"),
    ("dash:health", "Health"),
    ("dash:loops", "Loops"),
    ("dash:sessions", "Sessions"),
    ("dash:presets", "Schedule"),
    ("dash:settings", "Settings"),
    ("dash:interchange", "Import / export"),
)

#: The phrase an operator must type to write a safety-posture key — one gesture whether the
#: key is edited on the settings page or arrives inside an uploaded dump.
SAFETY_CONFIRM_PHRASE = "change-safety-posture"


class NavContext(TypedDict):
    nav_items: tuple[tuple[str, str], ...]
    nav_active: str
    instance_label: str


def instance_label() -> str:
    """Which BOX this dashboard is — the configured label, else the hostname.

    Teatree runs on several machines whose dashboards are otherwise identical. The
    shipped default is empty rather than a machine name (a shipped constant cannot be
    one), so the hostname is the fallback an unconfigured box resolves to.
    """
    return get_effective_settings().dashboard_instance_label or socket.gethostname()


def nav_context(active: str) -> NavContext:
    """Nav bar context — the item list, which one is active, and which box this is."""
    return {"nav_items": NAV_ITEMS, "nav_active": active, "instance_label": instance_label()}


def is_htmx(request: "HttpRequest") -> bool:
    """Whether htmx issued this request — the fragment/document fork every mutation takes."""
    return request.headers.get("HX-Request") == "true"


def error_page(request: "HttpRequest", reason: str, *, back: str) -> "HttpResponse":
    """A refusal a reader can leave: the reason, the nav, and a link back to *back*.

    The pre-htmx path answered a plain-text 400 with no layout and no link, so a
    refused write stranded the operator on a white page with only the back button.
    """
    context = {**nav_context(back), "reason": reason, "back": back, "back_label": _nav_label(back)}
    return render(request, "dash/error.html", context, status=400)


def _nav_label(url_name: str) -> str:
    return next((label for name, label in NAV_ITEMS if name == url_name), "the dashboard")


def actor(request: "HttpRequest") -> str:
    """The audit actor for a request — the authenticated username, else ``anonymous``."""
    user = getattr(request, "user", None)
    if user is not None and user.is_authenticated:
        return user.get_username()
    return "anonymous"
