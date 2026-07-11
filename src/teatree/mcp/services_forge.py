"""Forge (github/gitlab) MCP tool groups — reads + gated issue writes (#3076).

Both forges satisfy the same :class:`~teatree.core.backend_protocols.CodeHostBackend`,
so one parametrized registrar serves both — the group is selected by the
declared :class:`~teatree.backends.types.Service`. The client is resolved
through :func:`teatree.core.backend_factory.code_host_from_overlay` (a core
seam), never a direct ``teatree.backends.github`` / ``gitlab`` import, so the
transport-boundary fitness test holds and every forge gate the factory wires
stays intact.

The wave-2 issue writes (``<forge>_issue_create/comment/close/update``) route
every outbound body through the SHARED core forge-write seam
(:func:`teatree.core.send_proxy.route_forge_write`) BEFORE the backend call — the
SAME seam the dream loop and the ``t3`` CLI writers use, so the public-repo leak
gate (refuses a customer codename bound for a public forge) and the #117
send-proxy chokepoint (per-overlay allowlist + redaction + one ``SendAudit`` row)
fire identically on every surface. A leaking or non-allowlisted write never
reaches the forge. The whole group registers only when its ``Service`` is
declared, so an undeclared forge exposes no write tool (fail-closed).
"""

from typing import Any

from asgiref.sync import sync_to_async
from mcp.server.fastmcp import FastMCP
from mcp.types import ToolAnnotations

from teatree.backends.types import Service
from teatree.core.backend_factory import code_host_from_overlay
from teatree.core.backend_protocols import CodeHostBackend
from teatree.core.send_proxy import route_forge_write
from teatree.mcp.service_resolver import resolve_declaring_overlay_client

_READ_ONLY = ToolAnnotations(readOnlyHint=True)
_WRITE = ToolAnnotations(readOnlyHint=False, destructiveHint=False)
_DESTRUCTIVE = ToolAnnotations(readOnlyHint=False, destructiveHint=True)


def _scrub_forge_body(service: Service, *, repo: str, text: str, action: str, target: str) -> str:
    """Return the body to post after the shared forge-write seam, or refuse.

    Thin MCP adapter over :func:`teatree.core.send_proxy.route_forge_write` — the
    core seam that runs the public-repo leak gate + the #117 send-proxy for EVERY
    forge write (MCP, dream loop, ``t3`` CLI). Kept here only to map the MCP
    layer's :class:`~teatree.backends.types.Service` to the seam's forge id; it
    raises :class:`~teatree.core.send_proxy.OutboundLeakError` /
    :class:`~teatree.core.send_proxy.SendBlockedError` (both ``RuntimeError``) so a
    leaking or non-allowlisted write is stopped before the backend call.
    """
    return route_forge_write(forge=service.value, repo=repo, text=text, action=action, target=target)


def _forge_client(service: Service) -> CodeHostBackend:
    return resolve_declaring_overlay_client(service, code_host_from_overlay, description=f"{service.value} code host")


def _pr_snapshot(service: Service, *, repo: str, pr_iid: int, pr_url: str) -> dict[str, Any]:
    client = _forge_client(service)
    merge_state = client.fetch_pr_merge_state(slug=repo, pr_id=pr_iid)
    approvals = client.get_mr_approvals(repo=repo, pr_iid=pr_iid)
    return {
        "open_state": client.get_pr_open_state(pr_url=pr_url).value,
        "state": merge_state.state,
        "merged": merge_state.is_merged,
        "merge_commit_oid": merge_state.merge_commit_oid,
        "draft": client.fetch_pr_is_draft(slug=repo, pr_id=pr_iid),
        "author": client.get_pr_author(pr_url=pr_url),
        "approvals_left": approvals["approvals_left"],
        "approved_by": approvals["approved_by"],
        "unresolved_resolvable": approvals["unresolved_resolvable"],
    }


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
    _register_search_reads(server, service, prefix)
    _register_pr_reads(server, service, prefix)
    _register_issue_writes(server, service, prefix)


def _register_issue_writes(server: FastMCP, service: Service, prefix: str) -> None:
    async def issue_create(repo: str, title: str, body: str, *, labels: list[str] | None = None) -> dict[str, Any]:
        def _create() -> dict[str, Any]:
            client = _forge_client(service)
            action = f"{prefix}_issue_create"
            clean_title = _scrub_forge_body(service, repo=repo, text=title, action=action, target=repo)
            clean_body = _scrub_forge_body(service, repo=repo, text=body, action=action, target=repo)
            # A label reaches the public forge too (GitHub auto-creates a missing one),
            # so it rides the same leak scrub — a banned term in a label refuses the create.
            clean_labels = (
                [_scrub_forge_body(service, repo=repo, text=label, action=action, target=repo) for label in labels]
                if labels
                else labels
            )
            return dict(client.create_issue(repo=repo, title=clean_title, body=clean_body, labels=clean_labels))

        return await sync_to_async(_create, thread_sensitive=True)()

    async def issue_comment(issue_url: str, body: str) -> dict[str, Any]:
        def _comment() -> dict[str, Any]:
            client = _forge_client(service)
            repo = client.repo_for_issue_url(issue_url)
            clean = _scrub_forge_body(service, repo=repo, text=body, action=f"{prefix}_issue_comment", target=issue_url)
            return dict(client.post_issue_comment(issue_url=issue_url, body=clean))

        return await sync_to_async(_comment, thread_sensitive=True)()

    async def issue_close(issue_url: str, *, comment: str = "") -> dict[str, Any]:
        def _close() -> dict[str, Any]:
            client = _forge_client(service)
            repo = client.repo_for_issue_url(issue_url)
            clean = _scrub_forge_body(
                service, repo=repo, text=comment, action=f"{prefix}_issue_close", target=issue_url
            )
            return dict(client.close_issue(issue_url=issue_url, comment=clean))

        return await sync_to_async(_close, thread_sensitive=True)()

    async def issue_update(issue_url: str, body: str) -> dict[str, Any]:
        def _update() -> dict[str, Any]:
            client = _forge_client(service)
            repo = client.repo_for_issue_url(issue_url)
            clean = _scrub_forge_body(service, repo=repo, text=body, action=f"{prefix}_issue_update", target=issue_url)
            return dict(client.update_issue(issue_url=issue_url, body=clean))

        return await sync_to_async(_update, thread_sensitive=True)()

    server.add_tool(issue_create, name=f"{prefix}_issue_create", annotations=_WRITE)
    server.add_tool(issue_comment, name=f"{prefix}_issue_comment", annotations=_WRITE)
    server.add_tool(issue_close, name=f"{prefix}_issue_close", annotations=_DESTRUCTIVE)
    server.add_tool(issue_update, name=f"{prefix}_issue_update", annotations=_WRITE)


def _register_pr_reads(server: FastMCP, service: Service, prefix: str) -> None:
    async def pr_list(repo: str, *, state: str = "", author: str = "") -> list[dict[str, Any]]:
        return await sync_to_async(
            lambda: _forge_client(service).list_prs(repo=repo, state=state, author=author),
            thread_sensitive=True,
        )()

    async def pr_diff(repo: str, pr_iid: int) -> list[dict[str, Any]]:
        return await sync_to_async(
            lambda: _forge_client(service).get_pr_diff(repo=repo, pr_iid=pr_iid), thread_sensitive=True
        )()

    async def pr_commits(repo: str, pr_iid: int) -> list[dict[str, Any]]:
        return await sync_to_async(
            lambda: _forge_client(service).list_pr_commits(repo=repo, pr_iid=pr_iid), thread_sensitive=True
        )()

    async def repo_get(repo: str) -> dict[str, Any]:
        return await sync_to_async(lambda: _forge_client(service).get_repo(repo=repo), thread_sensitive=True)()

    server.add_tool(pr_list, name=f"{prefix}_pr_list", annotations=_READ_ONLY)
    server.add_tool(pr_diff, name=f"{prefix}_pr_diff", annotations=_READ_ONLY)
    server.add_tool(pr_commits, name=f"{prefix}_pr_commits", annotations=_READ_ONLY)
    server.add_tool(repo_get, name=f"{prefix}_repo_get", annotations=_READ_ONLY)


def _register_search_reads(server: FastMCP, service: Service, prefix: str) -> None:
    async def issue_search(repo: str, query: str) -> list[dict[str, Any]]:
        return await sync_to_async(
            lambda: _forge_client(service).search_open_issues(repo=repo, query=query), thread_sensitive=True
        )()

    async def issue_list_assigned(assignee: str) -> list[dict[str, Any]]:
        return await sync_to_async(
            lambda: _forge_client(service).list_assigned_issues(assignee=assignee), thread_sensitive=True
        )()

    async def my_merged_prs(author: str, *, updated_after: str = "") -> list[dict[str, Any]]:
        return await sync_to_async(
            lambda: _forge_client(service).list_my_merged_prs(author=author, updated_after=updated_after or None),
            thread_sensitive=True,
        )()

    async def pr_get(repo: str, pr_iid: int, pr_url: str) -> dict[str, Any]:
        return await sync_to_async(
            lambda: _pr_snapshot(service, repo=repo, pr_iid=pr_iid, pr_url=pr_url), thread_sensitive=True
        )()

    server.add_tool(issue_search, name=f"{prefix}_issue_search", annotations=_READ_ONLY)
    server.add_tool(issue_list_assigned, name=f"{prefix}_issue_list_assigned", annotations=_READ_ONLY)
    server.add_tool(my_merged_prs, name=f"{prefix}_my_merged_prs", annotations=_READ_ONLY)
    server.add_tool(pr_get, name=f"{prefix}_pr_get", annotations=_READ_ONLY)


def _instructions(prefix: str) -> str:
    return (
        f"- {prefix}_current_user(): the authenticated handle on this forge.\n"
        f"- {prefix}_my_prs(author, updated_after): open PRs/MRs authored by *author*.\n"
        f"- {prefix}_review_requested(reviewer, updated_after): PRs/MRs awaiting *reviewer*.\n"
        f"- {prefix}_pr_author(pr_url) / {prefix}_pr_comments(repo, pr_iid): one PR's author / comments.\n"
        f"- {prefix}_pr_get(repo, pr_iid, pr_url): one PR's open/merge/draft state, author, and "
        f"approval snapshot in a single read.\n"
        f"- {prefix}_my_merged_prs(author, updated_after): merged PRs/MRs authored by *author* (sweeps).\n"
        f"- {prefix}_pr_list(repo, state, author): PRs/MRs on *repo*, filtered by state "
        f"(open/closed/merged) and author.\n"
        f"- {prefix}_pr_diff(repo, pr_iid): the PR's changed files with per-file diffs.\n"
        f"- {prefix}_pr_commits(repo, pr_iid): the commits on the PR.\n"
        f"- {prefix}_repo_get(repo): *repo* metadata (default branch, path, id).\n"
        f"- {prefix}_issue(issue_url) / {prefix}_issue_comments(issue_url): one issue and its comments.\n"
        f"- {prefix}_issue_search(repo, query): open issues in *repo* matching *query* (dup-check).\n"
        f"- {prefix}_issue_list_assigned(assignee): open issues assigned to *assignee*.\n"
        f"- {prefix}_issue_create(repo, title, body, labels): open an issue. Body, title + labels are "
        f"leak-scrubbed (a customer codename bound for a public forge is REFUSED) and #117-audited.\n"
        f"- {prefix}_issue_comment(issue_url, body): comment on an issue (same leak scrub + audit).\n"
        f"- {prefix}_issue_close(issue_url, comment): close an issue, optional audit comment first.\n"
        f"- {prefix}_issue_update(issue_url, body): replace an issue's body in place (same scrub + audit)."
    )


def register_github(server: FastMCP) -> None:
    _register(server, Service.GITHUB, "github")


def register_gitlab(server: FastMCP) -> None:
    _register(server, Service.GITLAB, "gitlab")


INSTRUCTIONS_GITHUB = _instructions("github")
INSTRUCTIONS_GITLAB = _instructions("gitlab")
