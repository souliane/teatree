"""Resolve a ticket's forge issue title (shared by the signal hook + backfill).

Cards show ``(no description)`` until a ticket carries a human title. The title
lives on the forge; ``core`` reaches the forge only through the backend
registry seam (never a direct ``teatree.backends`` import), so this fetches it
via :func:`get_backend_provider`. Synthetic loop keys (``scanning-news://`` …)
are not forge URLs and resolve to ``""``.
"""

from typing import TYPE_CHECKING

from teatree.core.backend_registry import get_backend_provider
from teatree.core.overlay_loader import get_overlay_for_ticket
from teatree.types import RawAPIDict

if TYPE_CHECKING:
    from teatree.core.models.ticket import Ticket

_HTTP_SCHEMES = ("http://", "https://")


def read_issue_title(issue: RawAPIDict) -> str:
    """The issue's ``title``, or ``""`` when absent or not a string."""
    title = issue.get("title")
    return title if isinstance(title, str) else ""


def fetch_issue_title(ticket: "Ticket") -> str:
    """Fetch *ticket*'s forge issue title, or ``""`` for a non-forge sentinel.

    Raises whatever the forge read raises (404, CLI failure, resolution error)
    so each caller decides how to degrade — the signal hook logs and drops it,
    the backfill sweep skips that one ticket and continues.
    """
    if not ticket.issue_url.startswith(_HTTP_SCHEMES):
        return ""
    overlay = get_overlay_for_ticket(ticket)
    host = get_backend_provider().get_code_host_for_url(overlay, ticket.issue_url)
    if host is None:
        return ""
    return read_issue_title(host.get_issue(ticket.issue_url))
