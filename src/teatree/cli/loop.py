"""``t3 loop`` — start, stop, status, and one-shot tick of the fat loop.

The loop runs as a Claude Code ``/loop`` slot; this CLI manages the
slot's lifecycle and exposes ``tick`` for out-of-band invocations
(tests, manual debugging). ``start`` spawns a Claude Code session
with the loop pre-registered; ``stop`` prints the slot id to unregister
from inside the session.

The ``tick`` subcommand delegates to the ``loop_tick`` Django management
command via subprocess — anything that touches the Django ORM must be a
management command, not a plain typer command with manual ``django.setup()``.
"""

import os
import shutil
import sys
from pathlib import Path

import typer

from teatree.loop.statusline import default_path

loop_app = typer.Typer(name="loop", help="Manage the long-lived fat loop.", no_args_is_help=True)


@loop_app.command("tick")
def tick_command(
    *,
    statusline_file: Path = typer.Option(
        None,
        "--statusline-file",
        help="Override the statusline output path (test hook).",
    ),
    overlay: str = typer.Option(
        "",
        "--overlay",
        help="Restrict scanning to the named overlay (default: scan every registered overlay).",
    ),
    json_output: bool = typer.Option(False, "--json", help="Emit the tick report as JSON."),
) -> None:
    """Run one tick: scan in parallel, dispatch, render statusline.

    Delegates to the ``loop_tick`` Django management command so that
    Django is bootstrapped by the management framework (not manual
    ``django.setup()``).  All heavy imports (ORM, backends, scanners)
    live in the management command module, not here.
    """
    import django  # noqa: PLC0415

    os.environ.setdefault("DJANGO_SETTINGS_MODULE", "teatree.settings")
    django.setup()

    from django.core.management import call_command  # noqa: PLC0415

    kwargs: dict[str, str | bool | None] = {}
    if statusline_file is not None:
        kwargs["statusline_file"] = str(statusline_file)
    if overlay:
        kwargs["overlay"] = overlay
    if json_output:
        kwargs["json_output"] = True
    call_command("loop_tick", **kwargs)


@loop_app.command("status")
def status_command() -> None:
    """Show the loop's last-rendered statusline."""
    target = default_path()
    if not target.is_file():
        typer.echo("No statusline rendered yet — run `t3 loop tick` first.")
        raise typer.Exit(code=1)
    typer.echo(target.read_text(encoding="utf-8"))


def _cadence_for_loop_slot() -> str:
    """Return the ``/loop <duration>`` argument from ``T3_LOOP_CADENCE`` (seconds, default 720)."""
    raw = os.environ.get("T3_LOOP_CADENCE", "720").strip() or "720"
    try:
        seconds = max(60, int(raw))
    except ValueError:
        seconds = 720
    if seconds % 60 == 0:
        return f"{seconds // 60}m"
    return f"{seconds}s"


def _stdin_is_terminal() -> bool:
    """Return whether stdin is a TTY — wrapped so tests can patch around ``runner.invoke``'s stdin replacement."""
    return sys.stdin.isatty()


@loop_app.command("start")
def start_command(
    *,
    print_only: bool = typer.Option(
        False,
        "--print-only",
        help="Print the /loop slot definition instead of spawning a Claude Code session.",
    ),
) -> None:
    """Spawn a Claude Code session with the fat loop pre-registered.

    Looks for ``claude`` on ``PATH`` and runs it with an initial
    ``/loop <cadence> !t3 loop tick`` prompt so the loop is registered
    before the user types anything. When ``claude`` is not available or
    the caller is already inside a Claude Code session, falls back to
    printing the slash command for manual entry.
    """
    cadence = _cadence_for_loop_slot()
    register_command = f"/loop {cadence} !t3 loop tick"

    if print_only or os.environ.get("CLAUDECODE") or not _stdin_is_terminal():
        typer.echo("Run this in your interactive Claude Code session to register the loop:")
        typer.echo(f"    {register_command}")
        typer.echo("")
        typer.echo("Override the cadence with `T3_LOOP_CADENCE=<seconds> t3 loop start` (default 720).")
        typer.echo("")
        typer.echo(
            "The tick runs scanners, dispatches signals, renders the statusline, and"
            " executes mechanical actions (ticket completions). Lines starting with"
            " `SPAWN_AGENT subagent=…` in the tick output are agent dispatches the"
            " session should fulfil via its `Agent` tool — each scanner dedups by"
            " state (e.g. last-seen SHA), so the same item never re-spawns."
        )
        return

    claude_bin = shutil.which("claude")
    if not claude_bin:
        typer.echo("`claude` not found on PATH. Install Claude Code, then run:")
        typer.echo(f"    {register_command}")
        raise typer.Exit(code=1)

    typer.echo(f"Starting Claude Code with `{register_command}` …")
    os.execv(claude_bin, [claude_bin, register_command])  # noqa: S606  # Path comes from shutil.which; no shell, no user-controlled input.


@loop_app.command("stop")
def stop_command() -> None:
    """Print the slot id to stop in the Claude Code session."""
    typer.echo("To stop the loop, run `/loop unregister t3-loop` in the Claude Code session.")
