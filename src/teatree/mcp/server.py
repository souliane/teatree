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

from teatree.config import get_effective_settings
from teatree.mcp import introspection, search

_READ_ONLY = ToolAnnotations(readOnlyHint=True)

_INSTRUCTIONS = (
    "Read-only structured search over teatree's internal model. Prefer these "
    "tools over shelling out to `t3 ... list` and parsing text. All tools are "
    "read-only; mutations go through the `t3` CLI.\n"
    "\n"
    "Tools:\n"
    "- command_search(query): which `t3` CLI leaf command to run for a task — "
    "path, help summary, and whether it emits --json. Use this FIRST when unsure "
    "which command exists.\n"
    "- ticket_get(ticket): one ticket's full detail (by pk / issue number / URL) "
    "incl. its visited-phase ledger.\n"
    "- ticket_list(overlay, state, kind, role, in_flight): enumerate tickets by "
    "lifecycle state — the mirror of `t3 <overlay> ticket list`.\n"
    "- ticket_search(text, overlay, state, kind, role, in_flight): free-text "
    "ticket search across url / description / context.\n"
    "- worktree_status(ticket, overlay, active_only): a ticket's or an overlay's "
    "worktrees with FSM state, branch, db, staleness.\n"
    "- pr_for_ticket(ticket): the pull requests recorded for a ticket.\n"
    "- task_list(overlay, status, phase, ticket): the autonomous loop's task "
    "queue — the mirror of `t3 <overlay> tasks list`.\n"
    "- loop_stats(overlay): task-status counts plus the dead-letter total.\n"
    "- incoming_event_recent(source, unprocessed_only): recent inbound platform "
    "events.\n"
    "- config_setting_get(key, overlay): a config setting's effective value, its "
    "source (db vs file/env), and scope.\n"
    "- gate_status(overlay): the review-gate and raw-merge gate state.\n"
    "- factory_signals(overlay, window_days): the five factory quality/velocity "
    "signals with fail-loud statuses and the verdict.\n"
    "- factory_score(overlay, window_days): the recipe-weighted factory score "
    "(registered only when factory_score_enabled is on)."
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


async def _ticket_list(
    *,
    overlay: str | None = None,
    state: str | None = None,
    in_flight: bool = False,
    limit: int = 50,
) -> list[dict[str, Any]]:
    """List tickets by lifecycle state — the mirror of ``t3 <overlay> ticket list``.

    Filter by ``state`` (the stage agents call the "phase"), scope to an
    ``overlay``, or set ``in_flight=true`` for the not-yet-delivered set. For
    free-text / kind / role search use ``ticket_search``; for one ticket's full
    detail use ``ticket_get``.
    """
    return await sync_to_async(search.ticket_list, thread_sensitive=True)(
        overlay=overlay,
        state=state,
        in_flight=in_flight,
        limit=limit,
    )


async def _ticket_get(ticket: str) -> dict[str, Any]:
    """One ticket's full detail by pk, issue number, URL, or repo key.

    Returns the base ticket fields plus ``visited_phases`` (the union of
    lifecycle phases recorded across the ticket's sessions). An empty object
    means the reference did not resolve.
    """
    return await sync_to_async(search.ticket_get, thread_sensitive=True)(ticket=ticket)


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


async def _factory_signals(*, overlay: str | None = None, window_days: int = 28) -> dict[str, Any]:
    """Derived-on-read factory quality/velocity signals over the trailing window.

    Returns the five signals (first-try-green, defect-escape, review-catch,
    merge-latency, repair-burn) with fail-loud statuses (``ok`` /
    ``insufficient_data`` / ``instrumentation_gap``), each signal's red-floor
    trip, and the top-level verdict (``ok`` / ``regressing`` / ``red``). Scope to
    an overlay with ``overlay``; widen the window with ``window_days``.
    """
    return await sync_to_async(search.factory_signals, thread_sensitive=True)(
        overlay=overlay,
        window_days=window_days,
    )


async def _factory_score(*, overlay: str | None = None, window_days: int = 28) -> dict[str, Any]:
    """The recipe-weighted factory score over the trailing window (read-only).

    Returns the aggregate (``None`` when untrustworthy), the ``ok`` / ``regressing``
    / ``red`` verdict, coverage vs the recipe floor, the recipe provenance
    (``recipe_sha`` + ``recipe_approved``), the snapshot deltas, and the per-signal
    contributions. Scope to an overlay with ``overlay``; widen with ``window_days``.
    """
    return await sync_to_async(search.factory_score, thread_sensitive=True)(
        overlay=overlay,
        window_days=window_days,
    )


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


async def _task_list(
    *,
    overlay: str | None = None,
    status: str | None = None,
    phase: str | None = None,
    ticket: str | None = None,
    limit: int = 50,
) -> list[dict[str, Any]]:
    """The autonomous loop's task queue — the mirror of ``t3 <overlay> tasks list``.

    ``status`` filters to one status (pending / claimed / completed / failed).
    ``phase`` matches any spelling of a phase (short verb or gerund). ``ticket``
    narrows to one ticket (pk / issue number / URL). ``overlay`` scopes the
    queue. Returns each task's phase, status, target, subject, and claim, newest
    first.
    """
    return await sync_to_async(search.task_list, thread_sensitive=True)(
        overlay=overlay,
        status=status,
        phase=phase,
        ticket=ticket,
        limit=limit,
    )


async def _config_setting_get(key: str, *, overlay: str | None = None) -> dict[str, Any]:
    """A config setting's effective value, its source (db vs file/env), and scope.

    ``key`` is the setting name (e.g. ``mode``, ``require_human_approval_to_merge``).
    ``overlay`` reads that overlay's scope; omitted reads the global scope. An
    unknown key is reported ``known=false`` rather than raising.
    """
    return await sync_to_async(introspection.config_setting_get, thread_sensitive=True)(key=key, overlay=overlay)


async def _gate_status(*, overlay: str | None = None) -> dict[str, Any]:
    """The review-gate and raw-merge gate state — the merge-governing gates at a glance.

    ``review_gate`` reports whether a human must approve a merge and the
    review-phase evidence gates; ``raw_merge_gate`` reports whether raw
    ``gh``/``glab`` merges are blocked. Scope to an overlay with ``overlay``.
    """
    return await sync_to_async(introspection.gate_status, thread_sensitive=True)(overlay=overlay)


async def _command_search(query: str, *, limit: int = 20) -> list[dict[str, Any]]:
    """Which `t3` CLI leaf command to run for a task — the discoverability read.

    Given a natural-language ``query`` returns the matching `t3` leaf commands,
    each with the full ``path``, a one-line ``summary``, and ``emits_json``
    (whether it exposes a ``--json`` / ``--format`` output to parse). Use this
    when unsure which command exists instead of guessing a subcommand.
    """
    return await sync_to_async(introspection.command_search, thread_sensitive=True)(query=query, limit=limit)


def build_server() -> FastMCP:
    """Assemble a fresh stdio MCP server with the read-only search tools registered.

    Returns a new instance on every call (no import-time global) so tests can
    build and introspect a server in isolation. Django must already be
    configured (the ``t3 mcp serve`` entry point calls ``ensure_django`` first).
    """
    server: FastMCP = FastMCP("teatree", instructions=_INSTRUCTIONS)
    server.add_tool(_command_search, name="command_search", annotations=_READ_ONLY)
    server.add_tool(_ticket_search, name="ticket_search", annotations=_READ_ONLY)
    server.add_tool(_ticket_list, name="ticket_list", annotations=_READ_ONLY)
    server.add_tool(_ticket_get, name="ticket_get", annotations=_READ_ONLY)
    server.add_tool(_worktree_status, name="worktree_status", annotations=_READ_ONLY)
    server.add_tool(_pr_for_ticket, name="pr_for_ticket", annotations=_READ_ONLY)
    server.add_tool(_task_list, name="task_list", annotations=_READ_ONLY)
    server.add_tool(_loop_stats, name="loop_stats", annotations=_READ_ONLY)
    server.add_tool(_config_setting_get, name="config_setting_get", annotations=_READ_ONLY)
    server.add_tool(_gate_status, name="gate_status", annotations=_READ_ONLY)
    server.add_tool(_factory_signals, name="factory_signals", annotations=_READ_ONLY)
    server.add_tool(_incoming_event_recent, name="incoming_event_recent", annotations=_READ_ONLY)
    # T4-PR-2 — the recipe-weighted score is a DARK feature-flagged surface: it is
    # registered ONLY when factory_score_enabled is on, so the outer loop has no MCP
    # metric-to-beat until enablement is a deliberate act (the shipped OFF state
    # exposes no factory_score tool at all).
    if get_effective_settings().factory_score_enabled:
        server.add_tool(_factory_score, name="factory_score", annotations=_READ_ONLY)
    return server
