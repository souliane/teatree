"""Shared nav context + request helpers for the dashboard views (#3162)."""

import socket
from typing import TYPE_CHECKING, TypedDict

from teatree.config import get_effective_settings

if TYPE_CHECKING:
    from django.http import HttpRequest

# The top-level pages, in nav order: (url-name, label).
NAV_ITEMS: tuple[tuple[str, str], ...] = (
    ("dash:board", "Board"),
    ("dash:health", "Health"),
    ("dash:loops", "Loops"),
    ("dash:presets", "Schedule"),
    ("dash:config", "Config"),
    ("dash:settings", "Settings"),
)


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


def actor(request: "HttpRequest") -> str:
    """The audit actor for a request — the authenticated username, else ``anonymous``."""
    user = getattr(request, "user", None)
    if user is not None and user.is_authenticated:
        return user.get_username()
    return "anonymous"
