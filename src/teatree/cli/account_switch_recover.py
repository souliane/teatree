"""`t3 setup recover-account-switch` — the explicit `/login` recovery surface (#1916).

Runs the same detect-invalidate-reprobe cycle as the `t3 doctor` gate, but as a
standalone command the agent (or user) can invoke on demand after a `/login`,
without the rest of the doctor run. Exits non-zero only when a switch left a
connector unreachable so a caller (or CI) can gate on it.
"""

import typer

from teatree.utils.django_bootstrap import ensure_django


def _report_mcp_connectivity() -> bool:
    """Re-run the enabled-MCP connectivity check on the account-switch path (#2282).

    A `/login` switch can leave an enabled MCP server disconnected even when the
    messaging-backend reprobe passed, so the account-switch recovery surfaces the
    same connectivity findings the doctor gate does — including the degraded WARN
    (probe could not run) that the doctor path also prints. Returns ``True`` when
    every enabled server is connected (or the check degraded), ``False`` on a
    loud disconnection/provider finding.
    """
    from teatree.core.mcp_connectivity import check_mcp_connectivity  # noqa: PLC0415 — deferred: lazy CLI import

    outcome = check_mcp_connectivity()
    for finding in outcome.findings:
        typer.echo(f"  MCP: {finding}")
    return outcome.ok


def _report_reconnect_lines(*, open_links: bool) -> None:
    """Print one ``RECONNECT <name> -> <target>`` line per declared down connector (PR-19).

    After a switch leaves connectors unreachable, the manifest check names each
    declared-but-down claude.ai connector; recovery surfaces the exact reconnect
    target per connector so the operator (or agent) has a click-through path,
    not just a generic "re-auth in the UI". ``--open`` best-effort opens each URL
    (fail-open). A degraded probe (``claude`` absent) is silent here — the #2282
    check above already WARNed.
    """
    from teatree.cli.mcp import open_reconnect_targets  # noqa: PLC0415 — deferred: only the unreachable path needs it
    from teatree.core.connector_manifest import (  # noqa: PLC0415 — deferred post-bootstrap: walks overlays + probes MCP
        check_connector_manifest,
    )

    outcome = check_connector_manifest()
    if not outcome.down:
        return
    for line in outcome.reconnect_lines():
        typer.echo(f"  {line}")
    if open_links:
        urls = [d.requirement.reconnect_url for d in outcome.down if not d.requirement.instruction]
        opened = open_reconnect_targets(urls)
        typer.echo(f"  Opened {opened} reconnect URL(s) in a browser.")


def recover_account_switch(
    *,
    open_links: bool = typer.Option(
        False,
        "--open",
        help="Best-effort open each connector reconnect URL in a browser (fail-open).",
    ),
) -> None:
    """Detect a Claude account switch, invalidate the backend cache, re-probe connectors."""
    ensure_django()
    from teatree.core.account_switch import detect_and_recover_account_switch  # noqa: PLC0415 — lazy CLI import

    outcome = detect_and_recover_account_switch()
    if not outcome.switched:
        typer.echo(
            f"No account switch since last recovery (active {outcome.current_fingerprint[:8] or '?'}…).",
        )
        return

    typer.echo(
        f"Account switch: {outcome.previous_fingerprint[:8]}… → {outcome.current_fingerprint[:8]}…. "
        "Backend cache invalidated; re-probing connectors.",
    )
    for probe in outcome.probes:
        status = "reachable" if probe.reachable else f"UNREACHABLE — {probe.detail}"
        typer.echo(f"  {probe.name}: {status}")

    mcp_ok = _report_mcp_connectivity()

    if outcome.all_reachable and mcp_ok:
        typer.echo("All connectors reachable under the new account.")
        return

    typer.echo(
        "One or more connectors are unreachable. Re-auth the MCP connector(s) in the Claude.ai UI, then re-run.",
    )
    _report_reconnect_lines(open_links=open_links)
    raise typer.Exit(code=1)


__all__ = ["recover_account_switch"]
