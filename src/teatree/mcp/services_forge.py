"""Forge (github/gitlab) MCP tool groups — reads + gated issue writes (#3076).

Both forges satisfy the same :class:`~teatree.core.backend_protocols.CodeHostBackend`,
so one parametrized registrar serves both — the group is selected by the
declared :class:`~teatree.backends.types.Service`. The client is resolved
through :func:`teatree.core.backend_factory.code_host_from_overlay` (a core
seam), never a direct ``teatree.backends.github`` / ``gitlab`` import, so the
transport-boundary fitness test holds and every forge gate the factory wires
stays intact.

``<forge>_issue_create``, ``<forge>_issue_note`` and ``<forge>_issue_close`` are
entirely the #162 hygiene facade's (:mod:`teatree.core.issue_hygiene`) MCP face:
create judges the open backlog before it files (two-phase, so the dedupe cannot
be skipped), note routes a requirement into the DESCRIPTION where a lane reads
it, and close puts its rationale in a dated description section before closing
with no comment. The facade itself routes every outbound body through the
SHARED core forge-write seam (:func:`teatree.core.send_proxy.route_forge_write`)
— the SAME seam the dream loop and the ``t3`` CLI writers use — so the
public-repo leak gate, the self-authored-issue guard, and the #117 send-proxy
chokepoint fire identically on every surface. There is no blind whole-body
replace tool: the facade's only description mutation is a compare-and-append,
never a write computed from a stale read. The whole group registers only when
its ``Service`` is declared, so an undeclared forge exposes no write tool
(fail-closed).
"""

from itertools import starmap
from typing import TYPE_CHECKING, Any

from asgiref.sync import sync_to_async
from mcp.server.mcpserver import MCPServer
from mcp.server.mcpserver.exceptions import ToolError
from mcp.types import ToolAnnotations

from teatree.backends.types import Service
from teatree.core.backend_protocols import CodeHostBackend
from teatree.mcp.service_resolver import FORGE_CLIENTS

if TYPE_CHECKING:
    from collections.abc import Callable

    from teatree.core.issue_hygiene import CreateDecision

_READ_ONLY = ToolAnnotations(read_only_hint=True)
_WRITE = ToolAnnotations(read_only_hint=False, destructive_hint=False)
_DESTRUCTIVE = ToolAnnotations(read_only_hint=False, destructive_hint=True)


def _forge_client(service: Service) -> CodeHostBackend:
    return FORGE_CLIENTS[service].resolve()


async def _run_hygiene_write(work: "Callable[[], dict[str, Any]]") -> dict[str, Any]:
    """Run *work* off-thread, translating a facade refusal into a ``ToolError``.

    Under mcp 2.1+ any exception that is not a ``ToolError`` reaches the caller as
    ``UnexpectedToolError("Error executing tool <name>")`` with the reason left on
    the server (see ``tests/teatree_mcp/test_services_slack.py``) — an agent calling
    a hygiene write tool would see an opaque failure instead of the refusal it needs
    to self-correct (missing judgment, external ticket, sweep comment, leak block).
    """
    from teatree.core.issue_hygiene import (  # noqa: PLC0415 — deferred: ORM-adjacent import
        IssueWriteConflictError,
        SweepCommentRefusedError,
    )
    from teatree.core.self_forge_identities import (  # noqa: PLC0415 — deferred: ORM-adjacent import
        ExternalIssueRefusedError,
    )
    from teatree.core.send_proxy import OutboundBlockedError  # noqa: PLC0415 — deferred: ORM-adjacent import

    try:
        return await sync_to_async(work, thread_sensitive=True)()
    except (
        ExternalIssueRefusedError,
        SweepCommentRefusedError,
        IssueWriteConflictError,
        OutboundBlockedError,
        ValueError,
    ) as exc:
        raise ToolError(str(exc)) from exc


def _pr_snapshot(service: Service, *, repo: str, pr_iid: int, pr_url: str) -> dict[str, Any]:
    client = _forge_client(service)
    merge_state = client.fetch_pr_merge_state(slug=repo, pr_id=pr_iid)
    approvals = client.get_mr_approvals(repo=repo, pr_iid=pr_iid)
    return {
        "open_state": client.get_pr_open_state(pr_url=pr_url).value,
        "state": merge_state.state,
        "merged": merge_state.is_merged,
        "merge_commit_oid": merge_state.merge_commit_oid,
        "draft": client.fetch_pr_draft_state(slug=repo, pr_id=pr_iid).value,
        "author": client.get_pr_author(pr_url=pr_url),
        "approvals_left": approvals["approvals_left"],
        "approved_by": approvals["approved_by"],
        "unresolved_resolvable": approvals["unresolved_resolvable"],
    }


def _register(server: MCPServer, service: Service, prefix: str) -> None:
    async def current_user() -> str:
        """The authenticated handle on this forge."""
        return await sync_to_async(lambda: _forge_client(service).current_user(), thread_sensitive=True)()

    async def my_prs(author: str, *, updated_after: str = "") -> list[dict[str, Any]]:
        """Open PRs/MRs authored by *author*; *updated_after* keeps only those updated since."""
        return await sync_to_async(
            lambda: _forge_client(service).list_my_prs(author=author, updated_after=updated_after or None),
            thread_sensitive=True,
        )()

    async def review_requested(reviewer: str, *, updated_after: str = "") -> list[dict[str, Any]]:
        """PRs/MRs awaiting *reviewer*."""
        return await sync_to_async(
            lambda: _forge_client(service).list_review_requested_prs(
                reviewer=reviewer, updated_after=updated_after or None
            ),
            thread_sensitive=True,
        )()

    async def pr_author(pr_url: str) -> str:
        """One PR's author."""
        return await sync_to_async(lambda: _forge_client(service).get_pr_author(pr_url=pr_url), thread_sensitive=True)()

    async def pr_comments(repo: str, pr_iid: int) -> list[dict[str, Any]]:
        """One PR's comments."""
        return await sync_to_async(
            lambda: _forge_client(service).list_pr_comments(repo=repo, pr_iid=pr_iid), thread_sensitive=True
        )()

    async def issue(issue_url: str) -> dict[str, Any]:
        """One issue."""
        return await sync_to_async(lambda: _forge_client(service).get_issue(issue_url), thread_sensitive=True)()

    async def issue_comments(issue_url: str) -> list[dict[str, Any]]:
        """One issue's comments."""
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


def _candidate_summary(url: str, issue: dict[str, Any]) -> dict[str, Any]:
    """The minimum an agent needs to judge one open candidate without a second read."""
    return {
        "url": url,
        "title": str(issue.get("title") or ""),
        "updated_at": str(issue.get("updated_at") or ""),
        "labels": issue.get("labels") or [],
    }


def _create_decisions(raw: list[dict[str, Any]]) -> list["CreateDecision"]:
    """Read the agent's judgments off the wire into the facade's own type."""
    from teatree.core.issue_hygiene import CreateDecision  # noqa: PLC0415 — deferred: ORM-adjacent import

    return [
        CreateDecision(
            candidate_url=str(entry.get("url") or ""),
            fits=bool(entry.get("fits")),
            reason=str(entry.get("reason") or ""),
        )
        for entry in raw
    ]


def _register_issue_writes(server: MCPServer, service: Service, prefix: str) -> None:
    async def issue_create(
        repo: str,
        title: str,
        body: str,
        *,
        labels: list[str] | None = None,
        dedupe: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """File an issue — but only after the closest open tickets on *repo* have been judged.

        Two phases, because a dedupe an agent can skip is a dedupe that does not
        happen. Call it WITHOUT *dedupe* and nothing is written: the closest open tickets
        come back as ``candidates``, counts, and their ``snapshot``. Judge each
        one and call again with ``dedupe={"snapshot": [...], "decisions": [...]}`` —
        one ``fits`` folds the request into that ticket's description instead of
        filing, and every rejection's reason is kept in a private audit row. A ticket
        filed in between that enters the closest set makes the outcome ``stale_snapshot`` and you judge
        again. The two travel as one argument because a judgment set without the
        candidate set it was made against cannot be checked for completeness. Title, body and labels are
        leak-scrubbed (a customer codename bound for a public forge is REFUSED) and #117-audited.
        """

        def _prepare() -> dict[str, Any]:
            from teatree.core.issue_hygiene import (  # noqa: PLC0415 — deferred: ORM-adjacent import
                open_candidates,
                select_closest,
            )

            live = open_candidates(host=_forge_client(service), repo=repo)
            closest = select_closest(live, title, body)
            return {
                "outcome": "judgment_required",
                "candidates": list(starmap(_candidate_summary, closest.items())),
                "snapshot": list(closest),
                "open_count": len(live),
                "compared": len(closest),
                "instructions": (
                    "Nothing was written. Judge every returned candidate, then call again with dedupe={'snapshot': "
                    "<this snapshot>, 'decisions': [{'url': ..., 'fits': bool, 'reason': '<why not>'}]}. "
                    "One fits -> the request is appended to that ticket's description; none fits -> a new "
                    "ticket is filed with the supplied body; judgments are kept privately."
                ),
            }

        def _commit() -> dict[str, Any]:
            from teatree.core.issue_hygiene import IssueDraft, create_or_extend  # noqa: PLC0415 — deferred import

            draft = IssueDraft(
                repo=repo,
                title=title,
                body=body,
                labels=tuple(labels or ()),
                action=f"{prefix}_issue_create",
                forge=prefix,
            )
            outcome = create_or_extend(
                host=_forge_client(service),
                draft=draft,
                decisions=_create_decisions(dedupe.get("decisions") or [] if dedupe else []),
                snapshot_urls=dedupe.get("snapshot") if dedupe else None,
            )
            return {"outcome": outcome.kind, "issue_url": outcome.issue_url, "unjudged": list(outcome.unjudged)}

        work = _prepare if dedupe is None else _commit
        return await _run_hygiene_write(work)

    async def issue_note(issue_url: str, purpose: str, body: str, *, sweep_run_id: str = "") -> dict[str, Any]:
        """Record a note where its *purpose* belongs — description for a requirement, comment for status.

        requirement / change_request / scope_change / decision append a dated description section (a lane
        reads the description, never the comments); status / evidence post a comment. A *sweep_run_id*
        refuses every comment. Only the owner's / factory bot's own issues may be changed.
        """

        def _note() -> dict[str, Any]:
            from teatree.core.issue_hygiene import record_issue_note  # noqa: PLC0415 — deferred: ORM-adjacent import

            outcome = record_issue_note(
                host=_forge_client(service),
                issue_url=issue_url,
                purpose=purpose,
                content=body,
                sweep_run_id=sweep_run_id,
            )
            return {"issue_url": outcome.issue_url, "outcome": outcome.kind, "purpose": outcome.purpose.value}

        return await _run_hygiene_write(_note)

    async def issue_close(issue_url: str, *, rationale: str) -> dict[str, Any]:
        """Close an issue via the hygiene facade: the reason lands in the description, never a comment (#162).

        Routes through :func:`teatree.core.issue_hygiene.close_with_rationale` —
        the same self-author guard, leak scrub and #117 audit every other write
        in this module gets, so this can no longer be the raw untyped-comment
        seam ``close_issue(comment=...)`` was. There is no untyped body-replace tool; use the
        sibling ``*_issue_note`` instead.
        """

        def _close() -> dict[str, Any]:
            from teatree.core.issue_hygiene import close_with_rationale  # noqa: PLC0415 — deferred: ORM-adjacent import

            outcome = close_with_rationale(
                host=_forge_client(service),
                issue_url=issue_url,
                rationale=rationale,
                action=f"{prefix}_issue_close",
            )
            return {"issue_url": outcome.issue_url, "outcome": outcome.kind}

        return await _run_hygiene_write(_close)

    server.add_tool(issue_create, name=f"{prefix}_issue_create", annotations=_WRITE)
    server.add_tool(issue_note, name=f"{prefix}_issue_note", annotations=_WRITE)
    server.add_tool(issue_close, name=f"{prefix}_issue_close", annotations=_DESTRUCTIVE)


def _register_pr_reads(server: MCPServer, service: Service, prefix: str) -> None:
    async def pr_list(repo: str, *, state: str = "", author: str = "") -> list[dict[str, Any]]:
        """PRs/MRs on *repo*, filtered by state (open/closed/merged) and author."""
        return await sync_to_async(
            lambda: _forge_client(service).list_prs(repo=repo, state=state, author=author),
            thread_sensitive=True,
        )()

    async def pr_diff(repo: str, pr_iid: int) -> list[dict[str, Any]]:
        """The PR's changed files with per-file diffs."""
        return await sync_to_async(
            lambda: _forge_client(service).get_pr_diff(repo=repo, pr_iid=pr_iid), thread_sensitive=True
        )()

    async def pr_commits(repo: str, pr_iid: int) -> list[dict[str, Any]]:
        """The commits on the PR."""
        return await sync_to_async(
            lambda: _forge_client(service).list_pr_commits(repo=repo, pr_iid=pr_iid), thread_sensitive=True
        )()

    async def repo_get(repo: str) -> dict[str, Any]:
        """*repo* metadata (default branch, path, id)."""
        return await sync_to_async(lambda: _forge_client(service).get_repo(repo=repo), thread_sensitive=True)()

    server.add_tool(pr_list, name=f"{prefix}_pr_list", annotations=_READ_ONLY)
    server.add_tool(pr_diff, name=f"{prefix}_pr_diff", annotations=_READ_ONLY)
    server.add_tool(pr_commits, name=f"{prefix}_pr_commits", annotations=_READ_ONLY)
    server.add_tool(repo_get, name=f"{prefix}_repo_get", annotations=_READ_ONLY)


def _register_search_reads(server: MCPServer, service: Service, prefix: str) -> None:
    async def issue_search(repo: str, query: str) -> list[dict[str, Any]]:
        """Open issues in *repo* matching *query* (dup-check)."""
        return await sync_to_async(
            lambda: _forge_client(service).search_open_issues(repo=repo, query=query), thread_sensitive=True
        )()

    async def issue_list_assigned(assignee: str) -> list[dict[str, Any]]:
        """Open issues assigned to *assignee*."""
        return await sync_to_async(
            lambda: _forge_client(service).list_assigned_issues(assignee=assignee), thread_sensitive=True
        )()

    async def my_merged_prs(author: str, *, updated_after: str = "") -> list[dict[str, Any]]:
        """Merged PRs/MRs authored by *author* (sweeps)."""
        return await sync_to_async(
            lambda: _forge_client(service).list_my_merged_prs(author=author, updated_after=updated_after or None),
            thread_sensitive=True,
        )()

    async def pr_get(repo: str, pr_iid: int, pr_url: str) -> dict[str, Any]:
        """One PR's open/merge/draft state, author, and approval snapshot in a single read."""
        return await sync_to_async(
            lambda: _pr_snapshot(service, repo=repo, pr_iid=pr_iid, pr_url=pr_url), thread_sensitive=True
        )()

    server.add_tool(issue_search, name=f"{prefix}_issue_search", annotations=_READ_ONLY)
    server.add_tool(issue_list_assigned, name=f"{prefix}_issue_list_assigned", annotations=_READ_ONLY)
    server.add_tool(my_merged_prs, name=f"{prefix}_my_merged_prs", annotations=_READ_ONLY)
    server.add_tool(pr_get, name=f"{prefix}_pr_get", annotations=_READ_ONLY)


def register_github(server: MCPServer) -> None:
    _register(server, Service.GITHUB, "github")


def register_gitlab(server: MCPServer) -> None:
    _register(server, Service.GITLAB, "gitlab")
