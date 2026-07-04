"""``t3 mcp serve`` — run teatree's read-only structured-search MCP server.

A stdio MCP server an agent adds to its ``mcp.json`` to query teatree's internal
model (tickets, worktrees, PRs, the loop task queue, inbound events) as typed
tool calls instead of shelling out to ``t3 ... list`` and parsing text. Django is
bootstrapped here (the ORM-touching server import is deferred until after
``ensure_django``, the same shape as ``t3 cost``).
"""

import os

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


@mcp_app.command(name="browser-diagnosis")
def browser_diagnosis() -> None:
    """Report the optional chrome-devtools MCP registration (default off).

    Prints whether the browser-diagnosis MCP server is enabled and, when it is,
    the exact ``claude mcp add`` line that registers it — so an agent can inspect
    a deployed page's network/console/DOM before proposing a root cause for
    browser-visible breakage. No enforcement; a diagnostic aid only.
    """
    ensure_django()

    from teatree.core.browser_diagnosis import (  # noqa: PLC0415 — deferred post-bootstrap: reads a Django setting
        resolve_browser_diagnosis,
    )

    registration = resolve_browser_diagnosis(os.environ.get("T3_OVERLAY_NAME") or None)
    typer.echo(registration.message)
