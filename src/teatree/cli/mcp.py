"""``t3 mcp serve`` — run teatree's structured-search + gate-preserving-write MCP server.

A stdio MCP server an agent adds to its ``mcp.json`` to query teatree's internal
model (tickets, worktrees, PRs, the loop task queue, inbound events) as typed
tool calls instead of shelling out to ``t3 ... list`` and parsing text, and to
write through the same gated seams the CLI uses. ``--read-only`` serves the read
tools plus any write tool an ``--allow-write`` names. Django is bootstrapped here
(the ORM-touching server import is deferred until after ``ensure_django``, the
same shape as ``t3 cost``).
"""

from typing import Annotated

import typer

from teatree.cli.mcp_owning_domain import delegate_to_owning_domain
from teatree.core.mcp_registration import ALLOW_WRITE_ARG, READ_ONLY_ARG, read_only_serve_flags
from teatree.mcp.serve_lifecycle import reap_orphaned_servers, start_parent_death_watch
from teatree.utils.django_bootstrap import ensure_django

mcp_app = typer.Typer(
    name="mcp",
    no_args_is_help=True,
    help="MCP server exposing teatree's structured search and gate-preserving writes (stdio).",
)


@mcp_app.command()
def serve(
    *,
    read_only: Annotated[
        bool,
        typer.Option(
            READ_ONLY_ARG, help="Register only the read tools (what a headless phase without write access launches)."
        ),
    ] = False,
    allow_write: Annotated[
        list[str] | None,
        typer.Option(ALLOW_WRITE_ARG, help="A write tool a --read-only server still registers (repeatable)."),
    ] = None,
) -> None:
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
    allowed_writes = frozenset(allow_write or ())
    delegate_to_owning_domain(read_only_serve_flags(allowed_writes) if read_only else [])
    start_parent_death_watch()
    ensure_django()

    from teatree.cli.notion_mcp_seam import register as register_notion_seam  # noqa: PLC0415 — deferred: loads the SDK
    from teatree.mcp.server import build_server  # noqa: PLC0415 — deferred: keeps CLI startup light

    register_notion_seam()
    build_server(read_only=read_only, allowed_writes=allowed_writes).run("stdio")
