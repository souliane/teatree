"""Teatree's read-only structured-search MCP server (souliane/teatree#1023).

Exposes the internal model — tickets, worktrees, pull requests, the loop task
queue, inbound events — as MCP tools an agent can call directly, instead of
shelling out to ``t3 ... list`` and parsing text. Wired into the CLI as
``t3 mcp serve`` (stdio). Read-only: mutations stay on the FSM-guarded CLI.

- :mod:`teatree.mcp.serializers` — pure model -> JSON-dict projections
- :mod:`teatree.mcp.search` — sync ORM queries reusing the model managers
- :mod:`teatree.mcp.server` — FastMCP wiring (``build_server``)
"""

from teatree.mcp.server import build_server

__all__ = ["build_server"]
