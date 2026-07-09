"""Forge (github/gitlab) read-only MCP tool groups (#3076).

Both forges satisfy the same :class:`~teatree.core.backend_protocols.CodeHostBackend`,
so one parametrized registrar serves both — the group is selected by the
declared :class:`~teatree.backends.types.Service`. The client is resolved
through :func:`teatree.core.backend_factory.code_host_from_overlay` (a core
seam), never a direct ``teatree.backends.github`` / ``gitlab`` import, so the
transport-boundary fitness test holds and every forge gate the factory wires
stays intact.
"""

from typing import Any

from asgiref.sync import sync_to_async
from mcp.server.fastmcp import FastMCP
from mcp.types import ToolAnnotations

from teatree.backends.types import Service
from teatree.core.backend_factory import code_host_from_overlay
from teatree.core.backend_protocols import CodeHostBackend
from teatree.core.overlay_loader import get_all_overlays

_READ_ONLY = ToolAnnotations(readOnlyHint=True)


def _forge_client(service: Service) -> CodeHostBackend:
    for name, overlay in get_all_overlays().items():
        if service in overlay.config.required_third_party_services:
            host = code_host_from_overlay(name)
            if host is not None:
                return host
    msg = f"No registered overlay declares a configured {service.value} code host"
    raise RuntimeError(msg)


def _register(server: FastMCP, service: Service, prefix: str) -> None:
    async def current_user() -> str:
        return await sync_to_async(lambda: _forge_client(service).current_user(), thread_sensitive=True)()

    async def my_prs(author: str, *, updated_after: str = "") -> list[dict[str, Any]]:
        return await sync_to_async(
            lambda: _forge_client(service).list_my_prs(author=author, updated_after=updated_after or None),
            thread_sensitive=True,
        )()

    async def review_requested(reviewer: str, *, updated_after: str = "") -> list[dict[str, Any]]:
        return await sync_to_async(
            lambda: _forge_client(service).list_review_requested_prs(
                reviewer=reviewer, updated_after=updated_after or None
            ),
            thread_sensitive=True,
        )()

    async def pr_author(pr_url: str) -> str:
        return await sync_to_async(lambda: _forge_client(service).get_pr_author(pr_url=pr_url), thread_sensitive=True)()

    async def pr_comments(repo: str, pr_iid: int) -> list[dict[str, Any]]:
        return await sync_to_async(
            lambda: _forge_client(service).list_pr_comments(repo=repo, pr_iid=pr_iid), thread_sensitive=True
        )()

    async def issue(issue_url: str) -> dict[str, Any]:
        return await sync_to_async(lambda: _forge_client(service).get_issue(issue_url), thread_sensitive=True)()

    async def issue_comments(issue_url: str) -> list[dict[str, Any]]:
        return await sync_to_async(
            lambda: _forge_client(service).list_issue_comments(issue_url=issue_url), thread_sensitive=True
        )()

    server.add_tool(current_user, name=f"{prefix}_current_user", annotations=_READ_ONLY)
    server.add_tool(my_prs, name=f"{prefix}_my_prs", annotations=_READ_ONLY)
    server.add_tool(review_requested, name=f"{prefix}_review_requested", annotations=_READ_ONLY)
    server.add_tool(pr_author, name=f"{prefix}_pr_author", annotations=_READ_ONLY)
    server.add_tool(pr_comments, name=f"{prefix}_pr_comments", annotations=_READ_ONLY)
    server.add_tool(issue, name=f"{prefix}_issue", annotations=_READ_ONLY)
    server.add_tool(issue_comments, name=f"{prefix}_issue_comments", annotations=_READ_ONLY)


def _instructions(prefix: str) -> str:
    return (
        f"- {prefix}_current_user(): the authenticated handle on this forge.\n"
        f"- {prefix}_my_prs(author, updated_after): open PRs/MRs authored by *author*.\n"
        f"- {prefix}_review_requested(reviewer, updated_after): PRs/MRs awaiting *reviewer*.\n"
        f"- {prefix}_pr_author(pr_url) / {prefix}_pr_comments(repo, pr_iid): one PR's author / comments.\n"
        f"- {prefix}_issue(issue_url) / {prefix}_issue_comments(issue_url): one issue and its comments."
    )


def register_github(server: FastMCP) -> None:
    _register(server, Service.GITHUB, "github")


def register_gitlab(server: FastMCP) -> None:
    _register(server, Service.GITLAB, "gitlab")


INSTRUCTIONS_GITHUB = _instructions("github")
INSTRUCTIONS_GITLAB = _instructions("gitlab")
