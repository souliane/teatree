"""Sentry read-only MCP tool group — the per-service declaration pilot (#3076).

Registered by :func:`teatree.mcp.server.build_server` only when a registered
overlay declares ``Service.SENTRY`` in ``required_third_party_services``. The
client is built through :func:`teatree.core.backend_factory.sentry_client_from_overlay`
(a core seam), never a direct ``teatree.backends.sentry`` import, so the
transport-boundary fitness test holds. Org and token come from the first
declaring overlay with a configured ``sentry_org`` (token via the
``sentry_token_pass_key`` secret registration).
"""

from typing import TYPE_CHECKING, Any

from asgiref.sync import sync_to_async
from mcp.server.fastmcp import FastMCP
from mcp.types import ToolAnnotations

from teatree.backends.types import Service
from teatree.core.backend_factory import sentry_client_from_overlay
from teatree.mcp.service_resolver import resolve_declaring_overlay_client

if TYPE_CHECKING:
    from teatree.core.backend_registry import SentryReadClient

_READ_ONLY = ToolAnnotations(readOnlyHint=True)

INSTRUCTIONS = (
    "- sentry_top_issues(project, limit): the most frequent unresolved Sentry "
    "issues for a project.\n"
    "- sentry_issue_get(issue_id): one Sentry issue's summary.\n"
    "- sentry_issue_events(issue_id, limit): recent events for one issue.\n"
    "- sentry_projects(): the org's Sentry projects."
)


def _client() -> "SentryReadClient":
    return resolve_declaring_overlay_client(
        Service.SENTRY,
        sentry_client_from_overlay,
        description="Sentry org (sentry_org + sentry_token_pass_key)",
    )


async def _sentry_top_issues(project: str, *, limit: int = 10) -> list[dict[str, Any]]:
    """The most frequent unresolved Sentry issues for *project*, sorted by frequency."""
    return await sync_to_async(lambda: _client().get_top_issues(project=project, limit=limit), thread_sensitive=True)()


async def _sentry_issue_get(issue_id: str) -> dict[str, Any]:
    """One Sentry issue's summary (title, culprit, counts, status) by issue id."""
    return await sync_to_async(lambda: _client().get_issue(issue_id), thread_sensitive=True)()


async def _sentry_issue_events(issue_id: str, *, limit: int = 10) -> list[dict[str, Any]]:
    """Recent events for one Sentry issue — stack traces and context for triage."""
    return await sync_to_async(lambda: _client().get_issue_events(issue_id, limit=limit), thread_sensitive=True)()


async def _sentry_projects() -> list[dict[str, Any]]:
    """The declaring org's Sentry projects."""
    return await sync_to_async(lambda: _client().list_projects(), thread_sensitive=True)()


def register(server: FastMCP) -> None:
    server.add_tool(_sentry_top_issues, name="sentry_top_issues", annotations=_READ_ONLY)
    server.add_tool(_sentry_issue_get, name="sentry_issue_get", annotations=_READ_ONLY)
    server.add_tool(_sentry_issue_events, name="sentry_issue_events", annotations=_READ_ONLY)
    server.add_tool(_sentry_projects, name="sentry_projects", annotations=_READ_ONLY)
