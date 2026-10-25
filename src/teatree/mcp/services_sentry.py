"""Sentry MCP reads and a merged-PR-gated issue resolution tool.

Registered by :func:`teatree.mcp.server.build_server` only when a registered
overlay declares ``Service.SENTRY`` in ``required_third_party_services``. The
client is built through :func:`teatree.core.backend_factory.sentry_client_from_overlay`
(a core seam), never a direct ``teatree.backends.sentry`` import, so the
transport-boundary fitness test holds. Org and token come from the first
declaring overlay with a configured ``sentry_org`` (token via the
``sentry_token_pass_key`` secret registration).
"""

import re
from collections.abc import Callable
from typing import TYPE_CHECKING, Any
from urllib.parse import urlsplit

from asgiref.sync import sync_to_async
from mcp.server.mcpserver import MCPServer
from mcp.server.mcpserver.exceptions import ToolError
from mcp.types import ToolAnnotations

from teatree.backends.types import Service
from teatree.core.backend_protocols import CodeHostBackend
from teatree.mcp.service_resolver import FORGE_CLIENTS, SENTRY

if TYPE_CHECKING:
    from teatree.core.backend_registry import SentryIssueClient

_READ_ONLY = ToolAnnotations(read_only_hint=True)
_DESTRUCTIVE = ToolAnnotations(read_only_hint=False, destructive_hint=True)


def _client() -> "SentryIssueClient":
    return SENTRY.resolve()


def _forge_client(service: Service) -> CodeHostBackend:
    return FORGE_CLIENTS[service].resolve()


_BODY_URL = re.compile(r"""https?://[^\s<>()\[\]"']+""")


def _links_issue(body: str, permalink: str) -> bool:
    try:
        expected = urlsplit(permalink)
    except ValueError:
        return False
    identity = (expected.scheme.casefold(), expected.netloc.casefold(), expected.path.rstrip("/"))
    if not all(identity):
        return False
    for raw in _BODY_URL.findall(body):
        try:
            linked = urlsplit(raw.rstrip(".,;:!"))
        except ValueError:
            continue
        if (linked.scheme.casefold(), linked.netloc.casefold(), linked.path.rstrip("/")) == identity:
            return True
    return False


def _names_short_id(body: str, short_id: object) -> bool:
    return (
        isinstance(short_id, str)
        and bool(short_id)
        and bool(re.search(rf"(?<![\w-]){re.escape(short_id)}(?![\w-])", body))
    )


async def _read[T](operation: Callable[[], T]) -> T:
    try:
        return await sync_to_async(operation, thread_sensitive=True)()
    except Exception as exc:
        raise ToolError(str(exc)) from exc


async def _sentry_top_issues(project: str, *, limit: int = 10) -> list[dict[str, Any]]:
    """The most frequent unresolved Sentry issues for *project*, sorted by frequency."""
    return await _read(lambda: _client().get_top_issues(project=project, limit=limit))


async def _sentry_issue_get(issue_id: str) -> dict[str, Any]:
    """One Sentry issue's summary (title, culprit, counts, status) by issue id."""
    return await _read(lambda: _client().get_issue(issue_id))


async def _sentry_issue_events(issue_id: str, *, limit: int = 10) -> list[dict[str, Any]]:
    """Recent events for one Sentry issue — stack traces and context for triage."""
    return await _read(lambda: _client().get_issue_events(issue_id, limit=limit))


async def _sentry_projects() -> list[dict[str, Any]]:
    """The declaring org's Sentry projects."""
    return await _read(lambda: _client().list_projects())


async def _sentry_issue_resolve(issue_id: str, forge: str, repo: str, pr_iid: int) -> dict[str, Any]:
    """Resolve an issue only when a merged PR links its permalink or short ID."""

    def _resolve() -> dict[str, Any]:
        try:
            service = Service(forge)
        except ValueError as exc:
            msg = "forge must be github or gitlab"
            raise ToolError(msg) from exc
        if service not in FORGE_CLIENTS or not repo.strip() or pr_iid < 1:
            msg = "a configured github or gitlab repo and positive PR number are required"
            raise ToolError(msg)
        forge_client = _forge_client(service)
        merge = forge_client.fetch_pr_merge_state(slug=repo, pr_id=pr_iid)
        if not merge.is_merged or not merge.merge_commit_oid:
            msg = "the named fixing PR is not provably merged; Sentry issue remains unresolved"
            raise ToolError(msg)
        client = _client()
        issue = client.get_issue(issue_id)
        permalink = issue.get("permalink")
        message = forge_client.fetch_pr_message(slug=repo, pr_id=pr_iid)
        if message is None or not (
            (isinstance(permalink, str) and _links_issue(message.body, permalink))
            or _names_short_id(message.body, issue.get("shortId"))
        ):
            msg = "the merged PR body does not link this Sentry issue; resolution is refused"
            raise ToolError(msg)
        outcome = client.resolve_issue(issue_id)
        if outcome.get("status") != "resolved":
            msg = "Sentry issue did not read back as resolved"
            raise ToolError(msg)
        return {
            "id": str(outcome.get("id") or issue_id),
            "status": "resolved",
            "fixing_pr_merge_oid": merge.merge_commit_oid,
        }

    try:
        return await sync_to_async(_resolve, thread_sensitive=True)()
    except ToolError:
        raise
    except Exception as exc:
        raise ToolError(str(exc)) from exc


def register(server: MCPServer) -> None:
    server.add_tool(_sentry_top_issues, name="sentry_top_issues", annotations=_READ_ONLY)
    server.add_tool(_sentry_issue_get, name="sentry_issue_get", annotations=_READ_ONLY)
    server.add_tool(_sentry_issue_events, name="sentry_issue_events", annotations=_READ_ONLY)
    server.add_tool(_sentry_projects, name="sentry_projects", annotations=_READ_ONLY)
    server.add_tool(_sentry_issue_resolve, name="sentry_issue_resolve", annotations=_DESTRUCTIVE)
