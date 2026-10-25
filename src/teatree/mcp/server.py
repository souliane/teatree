"""MCP server wiring for teatree's structured search + gate-preserving writes.

:func:`build_server` assembles a fresh :class:`~mcp.server.mcpserver.MCPServer`
instance and registers the read tools (structured search over the internal
model) plus the gate-preserving write tools. The registered tools are thin
``async`` wrappers: MCPServer invokes a tool inside its running event loop, so each
wrapper crosses into Django's synchronous ORM through ``sync_to_async`` (the
framework-standard async-safe boundary) and returns the already-serialized JSON
the underlying function produced.

The full surface is NOT read-only. Alongside the read tools it exposes gate-preserving
write tools (:mod:`teatree.mcp.write_tools` — ``pr_create``, ``pr_merge``,
``notify_user``, ``config_setting_set`` … and the per-service forge/slack writes):
each write handler calls the exact seam the corresponding ``t3`` CLI command
calls, so every gate (shipping-phase FSM, sanctioned-merge keystone, on-behalf
verdict, leak scrub / send-proxy) fires identically on both surfaces. The read
tools carry ``read_only_hint``; the write tools name their gated seam in
:data:`teatree.mcp.write_tools.TOOL_SEAMS`.

``build_server(read_only=True)`` (``t3 mcp serve --read-only``, what a headless
phase without the ``mcp_write`` capability launches) registers only the tools
annotated ``read_only_hint`` — every core, service, mailbox and overlay tool passes
the one registration choke point, and a tool with no annotation counts as a write.
*allowed_writes* (``--allow-write``) names non-read-only tools such a server still
registers: a phase's deliverable tool or, for planning, reviewing and critic_reviewing,
the three peer-mailbox tools. Other read-only phases retain only mailbox self and peers.

The live agent mailbox is a separate, addressed communication surface, present
when the runner binds a task or an operator-authenticated attended child joins a worker.

A registered overlay contributes its own tools through
``overlay.connectors.mcp_tool_group()``, which hands over an
:class:`~teatree.core.mcp_tool_group.McpToolGroup` — the tools themselves, not a
registrar the server would have to trust. The server registers them on the same
terms it registers its own: a group whose ``requires`` names an undeclared
service is skipped, and a group carrying a write tool that names no gated seam is
refused whole, so an overlay can no more put an ungated mutation on this surface
than core can.
"""

import logging
from collections.abc import Awaitable, Callable
from inspect import signature
from typing import Any

from asgiref.sync import sync_to_async
from mcp.server.mcpserver import MCPServer
from mcp.types import ToolAnnotations

from teatree.agents.live_mailbox_client import MailboxClient as NeutralMailboxClient
from teatree.backends.types import Service
from teatree.core.factory.factory_score import FactoryScoreDict
from teatree.core.factory.factory_signals import FactorySignalsReportDict
from teatree.core.mcp_tool_group import ApprovalTier, WriteSeam
from teatree.core.overlay import McpToolGroup, OverlayBase
from teatree.core.overlay_loader import get_all_overlays
from teatree.mcp import (
    agent_mailbox,
    introspection,
    search,
    services_forge,
    services_notion,
    services_sentry,
    services_sharepoint,
    services_slack,
    tool_surface,
    write_tools,
)
from teatree.mcp.tool_surface import _NoShadowServer, _read_only_instructions

ToolContractError = tool_surface.ToolContractError
ToolNameCollisionError = tool_surface.ToolNameCollisionError

logger = logging.getLogger(__name__)

_READ_ONLY = ToolAnnotations(read_only_hint=True)


# Per-service tool groups, registered iff the service is in the union of
# ``required_third_party_services`` across all registered overlays.
_SERVICE_GROUPS: dict[Service, Callable[[MCPServer], None]] = {
    Service.GITHUB: services_forge.register_github,
    Service.GITLAB: services_forge.register_gitlab,
    Service.SLACK: services_slack.register,
    Service.NOTION: services_notion.register,
    Service.SENTRY: services_sentry.register,
    Service.SHAREPOINT: services_sharepoint.register,
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


def _group_of(name: str, overlay: OverlayBase) -> McpToolGroup | None:
    """*overlay*'s declared tool group, or ``None`` when it declares none or raises.

    An overlay resolves its own credentials and imports its own handlers in this
    hook, so it can fail for reasons that have nothing to do with the other
    overlays sharing the server. One broken contributor loses its own tools, never
    everyone's.
    """
    try:
        return overlay.connectors.mcp_tool_group()
    except Exception:
        logger.exception("overlay %r: mcp_tool_group() raised; its tools are not registered", name)
        return None


def _admits(name: str, group: McpToolGroup, declared: frozenset[Service]) -> bool:
    """Whether *group* may reach the surface, on the terms it declared.

    Two refusals, both fail-closed and both whole-group: a service no overlay
    declared (the per-service groups' own contract — no declaration, no tools),
    and a write tool naming no gated seam, which is the one thing this surface
    must never carry.
    """
    undeclared_services = sorted(str(service) for service in group.requires - declared)
    if undeclared_services:
        logger.warning(
            "overlay %r: mcp tool group requires undeclared service(s) %s; not registered",
            name,
            ", ".join(undeclared_services),
        )
        return False
    unguarded = group.undeclared_write_tools
    if unguarded:
        logger.error(
            "overlay %r: mcp tool(s) %s lack a gated seam or approval tier; group not registered",
            name,
            ", ".join(unguarded),
        )
        return False
    invalid: list[str] = []
    for tool in group.tools:
        try:
            dry_run = signature(tool.handler).parameters.get("dry_run")
        except (TypeError, ValueError):
            invalid.append(tool.name)
            continue
        unsafe_posture = bool(tool.is_write and tool.annotations and tool.annotations.destructive_hint) and (
            tool.tier is ApprovalTier.POSTURE
        )
        unsafe_default = dry_run is not None and dry_run.default is False
        if unsafe_posture or unsafe_default:
            invalid.append(tool.name)
    if invalid:
        logger.error("overlay %r: unsafe mcp tool contract(s) %s; group not registered", name, ", ".join(invalid))
        return False
    return True


def overlay_tool_groups(declared: frozenset[Service]) -> list[tuple[str, McpToolGroup]]:
    """The overlay tool groups admitted onto the surface, named by overlay, in name order."""
    return [
        (name, group)
        for name, overlay in sorted(get_all_overlays().items())
        if (group := _group_of(name, overlay)) is not None and _admits(name, group, declared)
    ]


def _write_tool_names(overlay_groups: list[tuple[str, McpToolGroup]]) -> frozenset[str]:
    overlay_writes = (tool.name for _, group in overlay_groups for tool in group.tools if tool.is_write)
    return frozenset(write_tools.TOOL_SEAMS).union(agent_mailbox.TOOL_SEAMS, overlay_writes)


def declared_write_tool_seams(declared: frozenset[Service]) -> dict[str, WriteSeam]:
    """Every write tool that names a gated seam — core's plus every admitted overlay's.

    The full seam allowlist for this surface: ``write_tools.TOOL_SEAMS`` is only
    core's half, and a guard that reads it alone cannot see an overlay's tools at all.
    """
    seams = dict(write_tools.TOOL_SEAMS)
    if agent_mailbox.MailboxClient.from_env() is not None:
        seams |= agent_mailbox.TOOL_SEAMS
    for _, group in overlay_tool_groups(declared):
        seams |= group.seams
    return seams


_PREAMBLE = (
    "Structured search + gate-preserving writes over teatree's internal model. "
    "Prefer these tools over shelling out to `t3 ... list` and parsing text; when unsure "
    "which `t3` command exists, call command_search first. "
    "the Teatree write tools each wrap the exact seam the `t3` CLI calls, so every FSM / "
    "merge / on-behalf / leak gate fires identically."
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


async def _factory_signals(*, overlay: str | None = None, window_days: int = 28) -> FactorySignalsReportDict:
    """Derived-on-read factory quality/velocity signals over the trailing window.

    Returns the signals (first-try-green, defect-escape, review-catch,
    merge-latency, PR start-to-merged, repair-burn) with fail-loud statuses (``ok`` /
    ``insufficient_data`` / ``instrumentation_gap``), each signal's red-floor
    trip, and the top-level verdict (``ok`` / ``regressing`` / ``red``). Scope to
    an overlay with ``overlay``; widen the window with ``window_days``.
    """
    return await sync_to_async(search.factory_signals, thread_sensitive=True)(
        overlay=overlay,
        window_days=window_days,
    )


async def _factory_score(*, overlay: str | None = None, window_days: int = 28) -> FactoryScoreDict:
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


_READ_TOOLS: dict[str, Callable[..., Awaitable[Any]]] = {
    "command_search": _command_search,
    "ticket_get": _ticket_get,
    "ticket_list": _ticket_list,
    "ticket_search": _ticket_search,
    "worktree_status": _worktree_status,
    "pr_for_ticket": _pr_for_ticket,
    "task_list": _task_list,
    "question_list": _question_list,
    "loop_stats": _loop_stats,
    "incoming_event_recent": _incoming_event_recent,
    "config_setting_get": _config_setting_get,
    "gate_status": _gate_status,
    "factory_signals": _factory_signals,
    "factory_score": _factory_score,
}


def build_server(
    *,
    read_only: bool = False,
    allowed_writes: frozenset[str] = frozenset(),
    mailbox_client: NeutralMailboxClient | None = None,
) -> MCPServer:
    """Assemble a fresh stdio MCP server with the read + gate-preserving write tools.

    Returns a new instance on every call (no import-time global) so tests can
    build and introspect a server in isolation. Django must already be
    configured (the ``t3 mcp serve`` entry point calls ``ensure_django`` first).
    *read_only* withholds every write tool not named in *allowed_writes* and drops
    its contributed overlay instruction line when present.
    """
    declared = _required_services()
    overlay_groups = overlay_tool_groups(declared)
    mailbox_client = mailbox_client or agent_mailbox.MailboxClient.from_env()

    instructions = (
        _PREAMBLE
        + (
            "\n\nLive agent mailbox (busy peers receive now; idle Claude peers can wake within their window):\n"
            + agent_mailbox.INSTRUCTIONS
            if mailbox_client is not None
            else ""
        )
        + "".join(f"\n\nOverlay tools ({name}):\n{group.instructions}" for name, group in overlay_groups)
        + "\n\nApproval tiers: read=read-only; agent=existing seam gates; posture=on-behalf posture or "
        "single-use approval; exact=approval bound to this exact write."
    )
    if read_only:
        instructions = _read_only_instructions(
            instructions, withheld=_write_tool_names(overlay_groups), allowed_writes=allowed_writes
        )
    write_seams = dict(write_tools.TOOL_SEAMS)
    if mailbox_client is not None:
        write_seams |= agent_mailbox.TOOL_SEAMS
    for _, group in overlay_groups:
        write_seams |= group.seams
    server = _NoShadowServer(
        "teatree",
        instructions=instructions,
        read_only=read_only,
        allowed_writes=allowed_writes,
        write_seams=write_seams,
    )
    for name, handler in _READ_TOOLS.items():
        server.add_tool(handler, name=name, annotations=_READ_ONLY)
    for service, register_group in sorted(_SERVICE_GROUPS.items()):
        if service in declared:
            register_group(server)
    write_tools.register(server)
    agent_mailbox.register(server, client=mailbox_client)
    # Last, and tool by tool rather than through a registrar the overlay owns:
    # the server is what puts a contributed tool on the surface, so a name it has
    # already given out is refused here rather than silently discarded by mcp.
    for _, group in overlay_groups:
        for tool in group.tools:
            server.add_tool(tool.handler, name=tool.name, annotations=tool.annotations)
    return server
