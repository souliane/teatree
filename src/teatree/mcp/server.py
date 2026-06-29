"""FastMCP server wiring for teatree's read-only structured search.

:func:`build_server` assembles a fresh :class:`~mcp.server.fastmcp.FastMCP`
instance and registers one tool per query in :mod:`teatree.mcp.search`. The
registered tools are thin ``async`` wrappers: FastMCP invokes a tool inside its
running event loop, so each wrapper crosses into Django's synchronous ORM through
``sync_to_async`` (the framework-standard async-safe boundary) and returns the
already-serialized JSON the search function produced.

The server exposes read-only tools only. Mutations stay on the FSM-guarded
``t3`` CLI so the orchestrator-decides / loop-executes topology is preserved.
"""

from typing import Any

from asgiref.sync import sync_to_async
from mcp.server.fastmcp import FastMCP
from mcp.types import ToolAnnotations

from teatree.mcp import search

_READ_ONLY = ToolAnnotations(readOnlyHint=True)

_INSTRUCTIONS = (
    "Read-only structured search over teatree's internal model — tickets, "
    "worktrees, pull requests, the autonomous loop's task queue, and inbound "
    "platform events. Prefer these tools over shelling out to `t3 ... list` and "
    "parsing text. All tools are read-only; mutations go through the `t3` CLI."
)


# ast-grep-ignore: ac-django-no-complexity-suppressions
async def _ticket_search(  # noqa: PLR0913 — MCP tool surface; each kwarg is a documented, keyword-only filter exposed 1:1 in the tool input schema, not an internal design smell.
    *,
    overlay: str | None = None,
    state: str | None = None,
    kind: str | None = None,
    role: str | None = None,
    text: str | None = None,
    in_flight: bool = False,
    limit: int = 50,
) -> list[dict[str, Any]]:
    """Search tickets by overlay, lifecycle state, kind, role, or free text.

    Set ``in_flight=true`` to list only tickets that are not yet delivered or
    ignored. ``text`` matches the issue URL, the short description, and the
    durable per-ticket context. Returns the matching tickets, newest first.
    """
    return await sync_to_async(search.ticket_search, thread_sensitive=True)(
        overlay=overlay,
        state=state,
        kind=kind,
        role=role,
        text=text,
        in_flight=in_flight,
        limit=limit,
    )


async def _worktree_status(
    *,
    ticket: str | None = None,
    overlay: str | None = None,
    active_only: bool = True,
) -> list[dict[str, Any]]:
    """Worktrees for a ticket, or an overlay's worktrees when no ticket is given.

    ``ticket`` accepts a pk, a bare issue number, or a full issue URL. Without a
    ticket, ``active_only`` (default) lists the overlay's in-flight worktrees.
    Returns each worktree's FSM state, branch, db name, and on-disk staleness.
    """
    return await sync_to_async(search.worktree_status, thread_sensitive=True)(
        ticket=ticket,
        overlay=overlay,
        active_only=active_only,
    )


async def _pr_for_ticket(ticket: str) -> list[dict[str, Any]]:
    """Pull requests recorded for a ticket (pk, issue number, or issue URL).

    Returns a list because a multi-repo ticket can carry several PRs.
    """
    return await sync_to_async(search.pr_for_ticket, thread_sensitive=True)(ticket=ticket)


async def _loop_stats(*, overlay: str | None = None) -> dict[str, Any]:
    """Autonomous-loop task counts by status, plus the dead-letter total.

    ``tasks`` counts pending / claimed / completed / failed tasks (optionally
    scoped to ``overlay``); ``dead_letter`` is the global count of reply
    dispatches that exhausted their retries.
    """
    return await sync_to_async(search.loop_stats, thread_sensitive=True)(overlay=overlay)


async def _incoming_event_recent(
    *,
    limit: int = 20,
    source: str | None = None,
    unprocessed_only: bool = False,
) -> list[dict[str, Any]]:
    """The most recent inbound platform events, newest first.

    ``source`` filters to one platform (slack / gitlab / github / notion / ci).
    Set ``unprocessed_only=true`` to list only events the dispatcher has not yet
    handled.
    """
    return await sync_to_async(search.incoming_event_recent, thread_sensitive=True)(
        limit=limit,
        source=source,
        unprocessed_only=unprocessed_only,
    )


def build_server() -> FastMCP:
    """Assemble a fresh stdio MCP server with the read-only search tools registered.

    Returns a new instance on every call (no import-time global) so tests can
    build and introspect a server in isolation. Django must already be
    configured (the ``t3 mcp serve`` entry point calls ``ensure_django`` first).
    """
    server: FastMCP = FastMCP("teatree", instructions=_INSTRUCTIONS)
    server.add_tool(_ticket_search, name="ticket_search", annotations=_READ_ONLY)
    server.add_tool(_worktree_status, name="worktree_status", annotations=_READ_ONLY)
    server.add_tool(_pr_for_ticket, name="pr_for_ticket", annotations=_READ_ONLY)
    server.add_tool(_loop_stats, name="loop_stats", annotations=_READ_ONLY)
    server.add_tool(_incoming_event_recent, name="incoming_event_recent", annotations=_READ_ONLY)
    return server
