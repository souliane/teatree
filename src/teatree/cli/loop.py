"""``t3 loop`` — start, stop, status, and one-shot tick of the fat loop.

The loop runs as a Claude Code ``/loop`` slot; this CLI manages the
slot's lifecycle and exposes ``tick`` for out-of-band invocations
(tests, manual debugging). Only ``tick`` and ``status`` are wired in
core; ``start``/``stop`` are environment-specific shims that subclasses
of this module fill in once a Claude-side loop binding exists.
"""

import json
from dataclasses import asdict
from pathlib import Path
from typing import Any

import typer

from teatree.core.backend_factory import code_host_from_overlay, messaging_from_overlay
from teatree.loop.statusline import default_path
from teatree.loop.tick import TickReport, run_tick

loop_app = typer.Typer(name="loop", help="Manage the long-lived fat loop.", no_args_is_help=True)

type ReportDict = dict[str, Any]


def _report_to_dict(report: TickReport) -> ReportDict:
    return {
        "started_at": report.started_at.isoformat(),
        "signal_count": report.signal_count,
        "action_count": report.action_count,
        "statusline_path": str(report.statusline_path) if report.statusline_path else "",
        "errors": report.errors,
        "actions": [asdict(action) for action in report.actions],
    }


@loop_app.command("tick")
def tick_command(
    *,
    statusline_file: Path = typer.Option(
        None, "--statusline-file", help="Override the statusline output path (test hook)."
    ),
    json_output: bool = typer.Option(False, "--json", help="Emit the tick report as JSON."),
) -> None:
    """Run one tick: scan in parallel, dispatch, render statusline."""
    host = code_host_from_overlay()
    messaging = messaging_from_overlay()
    report = run_tick(host=host, messaging=messaging, statusline_path=statusline_file)
    if json_output:
        typer.echo(json.dumps(_report_to_dict(report), indent=2))
        return
    typer.echo(f"OK    {report.signal_count} signal(s), {report.action_count} action(s).")
    if report.errors:
        for name, message in report.errors.items():
            typer.echo(f"WARN  {name}: {message}")
    if report.statusline_path:
        typer.echo(f"      statusline → {report.statusline_path}")


@loop_app.command("status")
def status_command() -> None:
    """Show the loop's last-rendered statusline."""
    target = default_path()
    if not target.is_file():
        typer.echo("No statusline rendered yet — run `t3 loop tick` first.")
        raise typer.Exit(code=1)
    typer.echo(target.read_text(encoding="utf-8"))


@loop_app.command("start")
def start_command() -> None:
    """Register the fat loop with the active Claude Code session.

    The actual ``/loop`` registration is environment-specific — this
    command emits the slot definition the user pastes into the Claude
    Code session's loop register.
    """
    typer.echo("Slot definition for `/loop`:")
    typer.echo("    name: t3-loop")
    typer.echo("    cadence_seconds: ${T3_LOOP_CADENCE:-720}")
    typer.echo("    body: |")
    typer.echo("        !t3 loop tick")


@loop_app.command("stop")
def stop_command() -> None:
    """Print the slot id to stop in the Claude Code session."""
    typer.echo("To stop the loop, run `/loop unregister t3-loop` in the Claude Code session.")
