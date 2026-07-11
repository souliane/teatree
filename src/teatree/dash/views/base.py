"""Shared nav context + request helpers for the dashboard views (#3162)."""

from typing import TYPE_CHECKING, TypedDict

if TYPE_CHECKING:
    from django.http import HttpRequest

# The three top-level pages, in nav order: (url-name, label).
NAV_ITEMS: tuple[tuple[str, str], ...] = (
    ("dash:board", "Board"),
    ("dash:health", "Health"),
    ("dash:loops", "Loops"),
)


class NavContext(TypedDict):
    nav_items: tuple[tuple[str, str], ...]
    nav_active: str


def nav_context(active: str) -> NavContext:
    """Nav bar context — the item list plus which one is active."""
    return {"nav_items": NAV_ITEMS, "nav_active": active}


def actor(request: "HttpRequest") -> str:
    """The audit actor for a request — the authenticated username, else ``anonymous``."""
    user = getattr(request, "user", None)
    if user is not None and user.is_authenticated:
        return user.get_username()
    return "anonymous"
