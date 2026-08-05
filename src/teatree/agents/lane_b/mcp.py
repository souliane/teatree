"""MCP wiring for Lane B — teatree's own read-only structured-search server.

Teatree ships a read-only MCP server (:mod:`teatree.mcp.server`) exposing
structured search over its internal model. Lane B mounts it as a pydantic_ai
``MCPToolset`` so the agent can query tickets/worktrees/PRs/tasks the same way
Lane A reaches the connector — mutations still stay on the FSM-guarded ``t3``
CLI (the server is read-only by construction).

The pydantic_ai MCP client needs the optional ``fastmcp`` extra
(``pydantic-ai-slim[mcp]``); when it is unavailable :func:`build_mcp_toolsets`
returns an empty list with a logged note rather than failing a dispatch, so the
tool layer degrades gracefully. Enabling it is NOT a one-line dependency add: the
extra resolves ``fastmcp-slim``, whose client imports ``mcp.McpError``, and the
``mcp>=2,<3`` this project pins renamed that to ``MCPError``. Adding the extra
therefore installs a ``fastmcp`` that cannot import — which is why availability is
decided by attempting the real import (:func:`mcp_client_available`) rather than by
module presence. Lane B keeps its Read/Write/Edit/Grep/Bash capabilities either
way; only the read-only structured-search toolset is withheld.
"""

import logging
from importlib import import_module
from importlib.util import find_spec

from pydantic_ai.toolsets.abstract import AbstractToolset

logger = logging.getLogger(__name__)

#: The stdio command that boots teatree's own read-only MCP server. A front-end
#: (or this harness) spawns it and speaks MCP over stdio.
TEATREE_MCP_STDIO_COMMAND: tuple[str, ...] = ("t3", "mcp", "serve")


def mcp_client_available() -> bool:
    """Whether ``pydantic_ai.mcp`` — the module this lane mounts — actually imports.

    The question is deliberately the real import rather than the presence of the
    ``fastmcp`` distribution that backs it. Those answers diverge in the state
    ``pydantic-ai-slim[mcp]`` produces against the ``mcp>=2,<3`` this project pins:
    the extra resolves ``fastmcp-slim``, whose client imports ``mcp.McpError`` — the
    name mcp 2.x renamed to ``MCPError`` — so a ``fastmcp`` module EXISTS while
    ``pydantic_ai.mcp`` raises ``ImportError``. A presence probe answers yes there,
    and the import below then propagates out of ``build_lane_b_toolsets``, killing
    every phased dispatch instead of degrading it. The cheap presence check runs
    first so the common no-extra install still short-circuits without an import.
    """
    if find_spec("fastmcp") is None:
        return False
    try:
        import_module("pydantic_ai.mcp")
    except ImportError:
        return False
    return True


def build_mcp_toolsets(*, command: tuple[str, ...] = TEATREE_MCP_STDIO_COMMAND) -> list[AbstractToolset[None]]:
    """Return the MCP toolsets for Lane B, or ``[]`` when the client is unavailable.

    An unavailable client degrades to ``[]`` (logged) so a dispatch still runs —
    with no MCP tools — rather than crashing. This is the only toolset the lane
    may lose this way: the capability toolsets it is composed with carry the
    read/write/shell surface and are built unconditionally.
    """
    if not mcp_client_available():
        logger.info(
            "Lane-B MCP disabled: the pydantic_ai MCP client is unavailable. Teatree's read-only MCP "
            "toolset needs the `pydantic-ai-slim[mcp]` extra, which cannot be installed while `mcp>=2,<3` "
            "is pinned (its `fastmcp-slim` client imports `mcp.McpError`, renamed `MCPError` in mcp 2.x). "
            "The dispatch keeps its Read/Write/Edit/Grep/Bash tools."
        )
        return []
    # Guarded by `mcp_client_available()`, which proved this exact import succeeds.
    from pydantic_ai.mcp import MCPServerStdio  # noqa: PLC0415 # ty: ignore[unresolved-import]

    server = MCPServerStdio(command[0], args=list(command[1:]))
    return [server]
