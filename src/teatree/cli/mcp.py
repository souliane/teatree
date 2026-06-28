"""``t3 mcp serve`` — run teatree's read-only structured-search MCP server.

A stdio MCP server an agent adds to its ``mcp.json`` to query teatree's internal
model (tickets, worktrees, PRs, the loop task queue, inbound events) as typed
tool calls instead of shelling out to ``t3 ... list`` and parsing text. Django is
bootstrapped here (the ORM-touching server import is deferred until after
``ensure_django``, the same shape as ``t3 cost``).
"""

import typer

from teatree.utils.django_bootstrap import ensure_django

mcp_app = typer.Typer(
    name="mcp",
    no_args_is_help=True,
    help="Read-only MCP server exposing teatree's structured search (stdio).",
)


@mcp_app.command()
def serve() -> None:
    """Run the structured-search MCP server over stdio (blocks until stdin closes)."""
    ensure_django()

    from teatree.mcp.server import build_server  # noqa: PLC0415

    build_server().run("stdio")
