"""FastMCP server wiring for teatree's structured search + gate-preserving writes.

:func:`build_server` assembles a fresh :class:`~mcp.server.fastmcp.FastMCP`
instance and registers the read tools (structured search over the internal
model) plus the gate-preserving write tools. The registered tools are thin
``async`` wrappers: FastMCP invokes a tool inside its running event loop, so each
wrapper crosses into Django's synchronous ORM through ``sync_to_async`` (the
framework-standard async-safe boundary) and returns the already-serialized JSON
the underlying function produced.

The surface is NOT read-only. Alongside the read tools it exposes gate-preserving
write tools (:mod:`teatree.mcp.write_tools` — ``pr_create``, ``pr_merge``,
``notify_user``, ``config_setting_set`` … and the per-service forge/slack writes):
each write handler calls the exact seam the corresponding ``t3`` CLI command
calls, so every gate (shipping-phase FSM, sanctioned-merge keystone, on-behalf
verdict, leak scrub / send-proxy) fires identically on both surfaces — the
orchestrator-decides / loop-executes topology is preserved through the seams, not
by withholding writes. The read tools carry ``readOnlyHint``; the write tools
name their gated seam in :data:`teatree.mcp.write_tools.TOOL_SEAMS`.
"""

from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any

from asgiref.sync import sync_to_async
from mcp.server.fastmcp import FastMCP
from mcp.types import ToolAnnotations

from teatree.backends.types import Service
from teatree.config import get_effective_settings
from teatree.core.factory.factory_signals import FactorySignalsReportDict
from teatree.core.overlay_loader import get_all_overlays
from teatree.mcp import (
    introspection,
    search,
    services_forge,
    services_notion,
    services_sentry,
    services_sharepoint,
    services_slack,
    write_tools,
)

_READ_ONLY = ToolAnnotations(readOnlyHint=True)

# Per-service tool groups, registered iff the service is in the union of
# ``required_third_party_services`` across all registered overlays. Each entry
# is ``(register, instructions)``; the instructions block is appended to the
# server instructions only when the group registers, so the instructions never
# advertise an unregistered tool.
_SERVICE_GROUPS: dict[Service, tuple[Callable[[FastMCP], None], str]] = {
    Service.GITHUB: (services_forge.register_github, services_forge.INSTRUCTIONS_GITHUB),
    Service.GITLAB: (services_forge.register_gitlab, services_forge.INSTRUCTIONS_GITLAB),
    Service.SLACK: (services_slack.register, services_slack.INSTRUCTIONS),
    Service.NOTION: (services_notion.register, services_notion.INSTRUCTIONS),
    Service.SENTRY: (services_sentry.register, services_sentry.INSTRUCTIONS),
    Service.SHAREPOINT: (services_sharepoint.register, services_sharepoint.INSTRUCTIONS),
}


def _required_services() -> frozenset[Service]:
    """The union of declared third-party services across all registered overlays.

    Reads the resolved ``OverlayConfig`` per overlay, so a DB ``overlays``-row
    override is already applied. Resolved once per server build — an overlay or
    override change needs an MCP server restart (same staleness contract as the
    flag-gated ``factory_score`` tool).
    """
    overlays = get_all_overlays()
    if not overlays:
        return frozenset()
    return frozenset().union(*(o.config.required_third_party_services for o in overlays.values()))


_PREAMBLE = (
    "Structured search + gate-preserving writes over teatree's internal model. "
    "Prefer these tools over shelling out to `t3 ... list` and parsing text. The "
    "read tools below are read-only; the write tools each wrap the exact seam the "
    "`t3` CLI calls, so every FSM / merge / on-behalf / leak gate fires identically "
    "(see the write-tool section at the end).\n"
    "\n"
    "Read tools:\n"
)


@dataclass(frozen=True)
class _ReadTool:
    """One read-only MCP tool: its name, async handler, and instruction line.

    The single source the base instructions and the registration loop both read,
    so a read tool's name/handler/prose can never drift across the two.
    """

    name: str
    handler: Callable[..., Awaitable[Any]]
    instruction: str


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


async def _factory_signals(*, overlay: str | None = None, window_days: int = 28) -> FactorySignalsReportDict:
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


async def _question_list(*, limit: int = 50) -> list[dict[str, Any]]:
    """Pending DeferredQuestion rows — the backlog awaiting the user's answer.

    The read counterpart to the ``question_answer`` write tool. Returns each
    pending question's id, text, raw options, originating session, and Slack
    ``ts`` / ``channel`` (when mirrored), newest first.
    """
    return await sync_to_async(introspection.question_list, thread_sensitive=True)(limit=limit)


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


# The always-registered read tools, in one table the base instructions and the
# registration loop both derive from (no INSTRUCTIONS/add_tool drift).
_READ_TOOLS: tuple[_ReadTool, ...] = (
    _ReadTool(
        "command_search",
        _command_search,
        "- command_search(query): which `t3` CLI leaf command to run for a task — "
        "path, help summary, and whether it emits --json. Use this FIRST when unsure "
        "which command exists.",
    ),
    _ReadTool(
        "ticket_get",
        _ticket_get,
        "- ticket_get(ticket): one ticket's full detail (by pk / issue number / URL) incl. its visited-phase ledger.",
    ),
    _ReadTool(
        "ticket_list",
        _ticket_list,
        "- ticket_list(overlay, state, kind, role, in_flight): enumerate tickets by "
        "lifecycle state — the mirror of `t3 <overlay> ticket list`.",
    ),
    _ReadTool(
        "ticket_search",
        _ticket_search,
        "- ticket_search(text, overlay, state, kind, role, in_flight): free-text "
        "ticket search across url / description / context.",
    ),
    _ReadTool(
        "worktree_status",
        _worktree_status,
        "- worktree_status(ticket, overlay, active_only): a ticket's or an overlay's "
        "worktrees with FSM state, branch, db, staleness.",
    ),
    _ReadTool("pr_for_ticket", _pr_for_ticket, "- pr_for_ticket(ticket): the pull requests recorded for a ticket."),
    _ReadTool(
        "task_list",
        _task_list,
        "- task_list(overlay, status, phase, ticket): the autonomous loop's task "
        "queue — the mirror of `t3 <overlay> tasks list`.",
    ),
    _ReadTool(
        "question_list",
        _question_list,
        "- question_list(limit): the pending DeferredQuestion backlog awaiting the "
        "user's answer (pairs with the question_answer write tool).",
    ),
    _ReadTool("loop_stats", _loop_stats, "- loop_stats(overlay): task-status counts plus the dead-letter total."),
    _ReadTool(
        "incoming_event_recent",
        _incoming_event_recent,
        "- incoming_event_recent(source, unprocessed_only): recent inbound platform events.",
    ),
    _ReadTool(
        "config_setting_get",
        _config_setting_get,
        "- config_setting_get(key, overlay): a config setting's effective value, its "
        "source (db vs file/env), and scope.",
    ),
    _ReadTool("gate_status", _gate_status, "- gate_status(overlay): the review-gate and raw-merge gate state."),
    _ReadTool(
        "factory_signals",
        _factory_signals,
        "- factory_signals(overlay, window_days): the five factory quality/velocity "
        "signals with fail-loud statuses and the verdict.",
    ),
)

_FACTORY_SCORE_TOOL = _ReadTool(
    "factory_score",
    _factory_score,
    "- factory_score(overlay, window_days): the recipe-weighted factory score "
    "(registered only when factory_score_enabled is on).",
)


def build_server() -> FastMCP:
    """Assemble a fresh stdio MCP server with the read + gate-preserving write tools.

    Returns a new instance on every call (no import-time global) so tests can
    build and introspect a server in isolation. Django must already be
    configured (the ``t3 mcp serve`` entry point calls ``ensure_django`` first).
    """
    declared = _required_services()
    # T4-PR-2 — the recipe-weighted score is a DARK feature-flagged surface: both
    # its tool registration and its instruction line are appended ONLY when
    # factory_score_enabled is on (the same fail-closed contract the per-service
    # groups honour — the instructions never advertise an unregistered tool), so
    # the outer loop has no MCP metric-to-beat until enablement is a deliberate act.
    score_on = get_effective_settings().factory_score_enabled
    read_tools = (*_READ_TOOLS, _FACTORY_SCORE_TOOL) if score_on else _READ_TOOLS

    instructions = (
        _PREAMBLE
        + "\n".join(tool.instruction for tool in read_tools)
        + "".join(
            f"\n\nDeclared-service tools ({service}):\n{group_instructions}"
            for service, (_, group_instructions) in sorted(_SERVICE_GROUPS.items())
            if service in declared
        )
        # The teatree-own write tools register UNCONDITIONALLY (unlike the
        # fail-closed per-service groups): each wraps a `t3` CLI seam that is
        # itself gate-guarded and no-ops safely when its backend is absent
        # (notify_user returns sent=false with no messaging backend), so a
        # service declaration is not their fail-closed lever — their seam is.
        + "\n\nTeatree write tools (gate-preserved — each wraps the seam the `t3` CLI calls):\n"
        + write_tools.INSTRUCTIONS
    )
    server: FastMCP = FastMCP("teatree", instructions=instructions)
    for tool in read_tools:
        server.add_tool(tool.handler, name=tool.name, annotations=_READ_ONLY)
    for service, (register_group, _) in sorted(_SERVICE_GROUPS.items()):
        if service in declared:
            register_group(server)
    write_tools.register(server)
    return server
