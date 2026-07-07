"""``t3 mcp serve`` — run teatree's read-only structured-search MCP server.

A stdio MCP server an agent adds to its ``mcp.json`` to query teatree's internal
model (tickets, worktrees, PRs, the loop task queue, inbound events) as typed
tool calls instead of shelling out to ``t3 ... list`` and parsing text. Django is
bootstrapped here (the ORM-touching server import is deferred until after
``ensure_django``, the same shape as ``t3 cost``).
"""

import os
from collections.abc import Callable

import typer

from teatree.utils.django_bootstrap import ensure_django

mcp_app = typer.Typer(
    name="mcp",
    no_args_is_help=True,
    help="Read-only MCP server exposing teatree's structured search (stdio).",
)


def open_reconnect_targets(reconnect_urls: list[str], *, opener: Callable[[str], object] | None = None) -> int:
    """Best-effort open each reconnect URL in a browser; return how many opened.

    Fail-open by design (PR-19): a browser that will not launch (headless CI, no
    display) must never fail the recovery command — the printed ``RECONNECT``
    lines are the real deliverable, ``--open`` is a convenience. Only ``http(s)``
    targets are opened; a non-URL instruction (a connector's bespoke re-auth step)
    is left for the human to read.
    """
    import webbrowser  # noqa: PLC0415 — deferred so importing the CLI never pulls a GUI lib.

    open_url = opener if opener is not None else webbrowser.open
    opened = 0
    for url in reconnect_urls:
        if not url.startswith("http"):
            continue
        try:
            open_url(url)
            opened += 1
        except Exception:  # noqa: BLE001, S112 — a failed browser launch must never fail recovery
            continue
    return opened


@mcp_app.command()
def serve() -> None:
    """Run the structured-search MCP server over stdio (blocks until stdin closes)."""
    ensure_django()

    from teatree.mcp.server import build_server  # noqa: PLC0415

    build_server().run("stdio")


@mcp_app.command()
def reconnect(
    *,
    open_links: bool = typer.Option(
        False,
        "--open",
        help="Best-effort open each reconnect URL in a browser (fail-open).",
    ),
) -> None:
    """Reconnect (or print exact steps for) every declared-but-down claude.ai connector.

    claude.ai-hosted connectors are re-authed in the claude.ai UI, not headlessly
    via ``claude mcp`` — so this prints one ``RECONNECT <name> -> <target>`` line
    per down connector across every registered overlay's manifest, and exits
    non-zero when a REQUIRED connector is down so a caller (or CI) can gate on it.
    """
    ensure_django()

    from teatree.core.connector_manifest import (  # noqa: PLC0415 — deferred post-ensure_django: walks overlays + probes MCP
        check_connector_manifest,
    )

    outcome = check_connector_manifest()
    if outcome.degraded:
        for finding in outcome.probe_findings:
            typer.echo(f"WARN  {finding}")
        return
    if not outcome.down:
        typer.echo("All declared connectors are connected.")
        return

    typer.echo(
        "Declared connectors need reconnecting (claude.ai connectors are re-authed in the UI, not headlessly):",
    )
    reconnect_urls = [d.requirement.reconnect_url for d in outcome.down if not d.requirement.instruction]
    for line in outcome.reconnect_lines():
        typer.echo(f"  {line}")
    if open_links:
        opened = open_reconnect_targets(reconnect_urls)
        typer.echo(f"Opened {opened} reconnect URL(s) in a browser.")
    if not outcome.ok:
        raise typer.Exit(code=1)


@mcp_app.command(name="browser-diagnosis")
def browser_diagnosis() -> None:
    """Report the chrome-devtools-mcp registration (the default browser tool, default on).

    Prints whether the chrome-devtools-mcp server is enabled and, when it is, the
    exact ``claude mcp add`` line that registers it — so an agent can drive and
    inspect a deployed page (navigate/click/fill, network/console/DOM) before
    proposing a root cause for browser-visible breakage. No enforcement; a
    diagnostic and interaction aid only.
    """
    ensure_django()

    from teatree.core.evidence.browser_diagnosis import (  # noqa: PLC0415 — deferred post-bootstrap: reads a Django setting
        resolve_browser_diagnosis,
    )

    registration = resolve_browser_diagnosis(os.environ.get("T3_OVERLAY_NAME") or None)
    typer.echo(registration.message)
