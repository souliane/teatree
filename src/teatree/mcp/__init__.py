"""Teatree's read-only structured-search MCP server (souliane/teatree#1023).

Exposes the internal model — tickets, worktrees, pull requests, the loop task
queue, inbound events — as MCP tools an agent can call directly, instead of
shelling out to ``t3 ... list`` and parsing text. Wired into the CLI as
``t3 mcp serve`` (stdio). Read-only: mutations stay on the FSM-guarded CLI.

- :mod:`teatree.mcp.serializers` — pure model -> JSON-dict projections
- :mod:`teatree.mcp.search` — sync ORM queries reusing the model managers
- :mod:`teatree.mcp.command_catalogue` — the `command_search` catalogue seam
    (CLI-provided, for discovering which `t3` command to run)
- :mod:`teatree.mcp.server` — FastMCP wiring (``build_server``)

``build_server`` is exported LAZILY (PEP 562 ``__getattr__``): importing the
``teatree.mcp`` package — which ``teatree.cli`` does at import time to reach the
``command_catalogue`` registration seam — must NOT eagerly pull ``server`` →
``search`` → the Django ORM before ``django.setup()`` has run. The lazy export
keeps ``from teatree.mcp import build_server`` working while deferring the
ORM-touching import to the ``t3 mcp serve`` path (which bootstraps Django first).
"""

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from teatree.mcp.server import build_server

__all__ = ["build_server"]


def __getattr__(name: str) -> object:
    if name == "build_server":
        from teatree.mcp.server import build_server  # noqa: PLC0415 — deferred so the package import stays ORM-free

        return build_server
    msg = f"module {__name__!r} has no attribute {name!r}"
    raise AttributeError(msg)
