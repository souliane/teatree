"""Refuse an unsandboxed Codex thread whose MCP servers the approval gate cannot judge."""

import asyncio
from collections.abc import Awaitable, Callable
from typing import Any

from teatree.agents.codex_app_server_options import CodexAppServerOptions, CodexAppServerRefusalError
from teatree.agents.harness_registry import HarnessFallbackError, HarnessFallbackKind

_MCP_STATUS_UNREADABLE = "its MCP status could not be read"
_MCP_STATUS_SILENT = "its MCP status did not answer in time"
MCP_PROBE_SECONDS = 10.0


async def refuse_unjudged_mcp_servers(
    request: Callable[[str, dict[str, Any]], Awaitable[dict[str, Any]]],
    thread_id: str,
    options: CodexAppServerOptions,
) -> None:
    """Refuse an unsandboxed thread that still loads any MCP server: its tool calls never reach the approval gate."""
    if options.sandbox_mode != "danger-full-access":
        return
    try:
        response = await asyncio.wait_for(
            request("mcpServerStatus/list", {"threadId": thread_id, "detail": "toolsAndAuthOnly"}),
            timeout=MCP_PROBE_SECONDS,
        )
    except CodexAppServerRefusalError as exc:
        raise _mcp_refusal(_MCP_STATUS_UNREADABLE, thread_id) from exc
    except TimeoutError as exc:
        raise _mcp_refusal(_MCP_STATUS_SILENT, thread_id) from exc
    servers = response.get("data")
    if not isinstance(servers, list):
        raise _mcp_refusal(_MCP_STATUS_UNREADABLE, thread_id)
    if servers or response.get("nextCursor"):
        names = sorted(str(server.get("name")) for server in servers if isinstance(server, dict))
        reason = f"it loads MCP servers the approval gate cannot judge: {', '.join(names) or 'unlisted'}"
        raise _mcp_refusal(reason, thread_id)


def _mcp_refusal(reason: str, thread_id: str) -> HarnessFallbackError:
    return HarnessFallbackError(
        f"Codex thread refused: {reason}.",
        kind=HarnessFallbackKind.ACCESS,
        side_effects_started=False,
        agent_session_id=thread_id,
    )
