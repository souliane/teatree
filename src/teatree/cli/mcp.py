"""``t3 mcp serve`` — run teatree's read-only structured-search MCP server.

A stdio MCP server an agent adds to its ``mcp.json`` to query teatree's internal
model (tickets, worktrees, PRs, the loop task queue, inbound events) as typed
tool calls instead of shelling out to ``t3 ... list`` and parsing text. Django is
bootstrapped here (the ORM-touching server import is deferred until after
``ensure_django``, the same shape as ``t3 cost``).
"""

import typer

from teatree.cli.mcp_owning_domain import delegate_to_owning_domain
from teatree.mcp.serve_lifecycle import reap_orphaned_servers, start_parent_death_watch
from teatree.utils.django_bootstrap import ensure_django

mcp_app = typer.Typer(
    name="mcp",
    no_args_is_help=True,
    help="Read-only MCP server exposing teatree's structured search (stdio).",
)


@mcp_app.command()
def serve() -> None:
    """Run the structured-search MCP server over stdio (blocks until stdin closes).

    Orphaned predecessors are reaped FIRST, because the handoff below is an ``execv``
    that replaces this image: a reap sequenced after it never runs on a delegating
    install, and the host is the only venue whose PID 1 proves a client is gone.

    This server writes, so it then hands itself to whichever domain owns the control
    database — a no-op on every install the containerized stack has not claimed (see
    :mod:`teatree.cli.mcp_owning_domain`) — and arms the parent-death watchdog so THIS
    server exits even when a leaked fd keeps its stdin from ever reaching EOF. See
    :mod:`teatree.mcp.serve_lifecycle`.
    """
    reap_orphaned_servers()
    delegate_to_owning_domain()
    start_parent_death_watch()
    ensure_django()

    from teatree.cli.notion_mcp_seam import register as register_notion_seam  # noqa: PLC0415 — deferred: loads the SDK
    from teatree.mcp.server import build_server  # noqa: PLC0415 — deferred: keeps CLI startup light

    register_notion_seam()
    build_server().run("stdio")
