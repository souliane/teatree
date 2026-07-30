"""MCP wiring for Lane B — teatree's own read-only structured-search server.

Teatree ships a read-only MCP server (:mod:`teatree.mcp.server`) exposing
structured search over its internal model. Lane B mounts it as a pydantic_ai
``MCPToolset`` so the agent can query tickets/worktrees/PRs/tasks the same way
Lane A reaches the connector — mutations still stay on the FSM-guarded ``t3``
CLI (the server is read-only by construction).

The pydantic_ai MCP client needs the optional ``fastmcp`` extra
(``pydantic-ai-slim[mcp]``); when it is absent :func:`build_mcp_toolsets` returns
an empty list with a logged note rather than failing a dispatch, so the tool
layer degrades gracefully on an install without the extra. Enabling real MCP is
a one-line dependency add, tracked as a follow-up.
"""

import logging
from importlib.util import find_spec

from pydantic_ai.toolsets.abstract import AbstractToolset

logger = logging.getLogger(__name__)

#: The stdio command that boots teatree's own read-only MCP server. A front-end
#: (or this harness) spawns it and speaks MCP over stdio.
TEATREE_MCP_STDIO_COMMAND: tuple[str, ...] = ("t3", "mcp", "serve")


def mcp_client_available() -> bool:
    """Whether the pydantic_ai MCP client dependency (``fastmcp``) is importable."""
    return find_spec("fastmcp") is not None


def build_mcp_toolsets(*, command: tuple[str, ...] = TEATREE_MCP_STDIO_COMMAND) -> list[AbstractToolset[None]]:
    """Return the MCP toolsets for Lane B, or ``[]`` when the client is absent.

    A missing ``fastmcp`` extra degrades to ``[]`` (logged once) so a dispatch on
    an install without it still runs — with no MCP tools — rather than crashing.
    """
    if not mcp_client_available():
        logger.info(
            "Lane-B MCP disabled: the pydantic_ai MCP client (`fastmcp`) is not installed; "
            "add the `pydantic-ai-slim[mcp]` extra to enable teatree's read-only MCP toolset."
        )
        return []
    # `pydantic_ai.mcp` only imports with the `fastmcp` extra; guarded above by
    # `mcp_client_available()`, so this line is unreachable without it.
    from pydantic_ai.mcp import MCPServerStdio  # noqa: PLC0415 # ty: ignore[unresolved-import]

    server = MCPServerStdio(command[0], args=list(command[1:]))
    return [server]
