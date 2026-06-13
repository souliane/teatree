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
    same connectivity findings the doctor gate does. Returns ``True`` when every
    enabled server is connected (or the check degraded), ``False`` on a loud
    disconnection/provider finding.
    """
    from teatree.core.mcp_connectivity import check_mcp_connectivity  # noqa: PLC0415

    outcome = check_mcp_connectivity()
    if outcome.ok:
        return True
    for finding in outcome.findings:
        typer.echo(f"  MCP: {finding}")
    return False


def recover_account_switch() -> None:
    """Detect a Claude account switch, invalidate the backend cache, re-probe connectors."""
    ensure_django()
    from teatree.core.account_switch import detect_and_recover_account_switch  # noqa: PLC0415

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
        "One or more connectors are unreachable. Re-auth the MCP connector(s) in the "
        "Claude.ai UI (and reconnect the Claude-in-Chrome extension per /t3:e2e), then re-run.",
    )
    raise typer.Exit(code=1)


__all__ = ["recover_account_switch"]
