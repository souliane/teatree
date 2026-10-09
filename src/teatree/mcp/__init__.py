"""Teatree's structured-search + gate-preserving-write MCP server (souliane/teatree#1023).

Exposes the internal model — tickets, worktrees, pull requests, the loop task
queue, inbound events — as read tools an agent can call directly, instead of
shelling out to ``t3 ... list`` and parsing text, PLUS gate-preserving write
tools (``pr_create`` / ``pr_merge`` / ``notify_user`` / ``config_setting_set`` …
and the per-service forge/slack writes). Wired into the CLI as ``t3 mcp serve``
(stdio). NOT read-only: each write handler calls the exact seam the ``t3`` CLI
calls, so the FSM / merge / on-behalf / leak gates fire identically on both
surfaces — the topology is preserved through the seams, not by withholding writes.

- :mod:`teatree.mcp.serializers` — pure model -> JSON-dict projections
- :mod:`teatree.mcp.search` — sync ORM queries reusing the model managers
- :mod:`teatree.mcp.command_catalogue` — the `command_search` catalogue seam
    (CLI-provided, for discovering which `t3` command to run)
- :mod:`teatree.mcp.server` — MCPServer wiring (``build_server``)

Import :func:`teatree.mcp.server.build_server` after Django setup. Importing
this package itself remains ORM-free for CLI command registration.
"""
