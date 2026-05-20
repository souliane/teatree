"""``t3 loop spawn-headless`` / ``install-watchdog`` / ``uninstall-watchdog`` (#1139).

The CLI shell over :mod:`teatree.loop.watchdog`. Kept separate from
``cli/loop.py`` so that file stays under the module-health public-function
cap (see the comment trailer in ``cli/loop.py``).
"""

import os
import shutil
import sys

import typer

from teatree.loop import watchdog


def _suggest_label() -> str:
    user = os.environ.get("USER", "user").replace(".", "-")
    return f"com.{user}.teatree-loop"


def spawn_headless_command() -> None:
    """Boot a Claude Code session with the loop pre-registered (idempotent).

    Exits 0 without doing anything when a healthy loop session is already
    running under the currently-active Claude Code account (no respawn
    needed). Otherwise launches ``claude`` with the ``/loop`` slot
    pre-registered and pins the spawned session under the active account.

    Designed to be invoked by ``launchd`` (macOS) with ``KeepAlive=true``:
    the LaunchAgent re-runs this command whenever the spawned ``claude``
    process exits, so a crash, ``/exit``, or terminal close triggers a
    respawn.
    """
    if not watchdog.needs_respawn():
        typer.echo("teatree loop already running under the active account; nothing to do.")
        return

    claude_bin = shutil.which("claude")
    if not claude_bin:
        typer.echo("`claude` not found on PATH. Install Claude Code, then re-run.", err=True)
        raise typer.Exit(code=1)

    # The CLAUDECODE env-var sentinel prevents recursive spawn if launchd
    # ever invoked this from inside a Claude Code session.
    if os.environ.get("CLAUDECODE"):
        typer.echo("Refusing to spawn-headless from inside an existing Claude Code session.", err=True)
        raise typer.Exit(code=1)

    register_command = (
        "/t3:teatree Run `t3 loop tick`, then repeatedly run `t3 loop claim-next --json`"
        " until it returns nothing. For each entry call the Agent tool with"
        " subagent_type=entry.subagent, description=entry.execution_reason,"
        " and a prompt that includes entry.issue_url."
    )
    typer.echo(f"Spawning headless Claude Code with `{register_command}` …")
    os.execv(claude_bin, [claude_bin, register_command])  # noqa: S606  # claude_bin comes from shutil.which.


def install_watchdog_command(
    *,
    label: str = typer.Option("", "--label", help="LaunchAgent label (default: com.$USER.teatree-loop)."),
    t3_bin: str = typer.Option("", "--t3-bin", help="Absolute path to the `t3` binary (default: autodetect)."),
) -> None:
    """Install the macOS LaunchAgent that keeps the loop session alive.

    Writes ``~/Library/LaunchAgents/<label>.plist`` and ``launchctl
    load``-s it. With ``KeepAlive=true`` and ``RunAtLoad=true``, launchd
    re-invokes ``t3 loop spawn-headless`` whenever the previous Claude
    Code session exits — including after a ``/login`` account switch.

    Linux is not yet supported by this command; print a cron suggestion
    for now.
    """
    if sys.platform != "darwin":
        typer.echo(
            "install-watchdog currently supports macOS only. Linux TODO — for a quick"
            " fallback, install this crontab line:",
            err=True,
        )
        typer.echo("    * * * * * pgrep -f 'claude' >/dev/null || t3 loop spawn-headless", err=True)
        raise typer.Exit(code=1)

    resolved_label = label or _suggest_label()
    resolved_t3 = t3_bin or shutil.which("t3") or "t3"

    try:
        path = watchdog.install_watchdog(label=resolved_label, t3_bin=resolved_t3)
    except watchdog.WatchdogError as exc:
        typer.echo(f"install-watchdog failed: {exc}", err=True)
        raise typer.Exit(code=1) from exc

    typer.echo(f"LaunchAgent installed at {path}")
    typer.echo(f"  label:  {resolved_label}")
    typer.echo(f"  t3 bin: {resolved_t3}")
    typer.echo("The agent will keep `t3 loop spawn-headless` running with KeepAlive.")


def uninstall_watchdog_command(
    *,
    label: str = typer.Option("", "--label", help="LaunchAgent label (default: com.$USER.teatree-loop)."),
) -> None:
    """Remove the macOS LaunchAgent installed by ``install-watchdog``."""
    if sys.platform != "darwin":
        typer.echo("uninstall-watchdog currently supports macOS only.", err=True)
        raise typer.Exit(code=1)

    resolved_label = label or _suggest_label()
    try:
        watchdog.uninstall_watchdog(label=resolved_label)
    except watchdog.WatchdogError as exc:
        typer.echo(f"uninstall-watchdog failed: {exc}", err=True)
        raise typer.Exit(code=1) from exc
    typer.echo(f"LaunchAgent {resolved_label} unloaded and removed.")


def register(loop_app: typer.Typer) -> None:
    """Attach the watchdog commands to the ``t3 loop`` Typer app."""
    loop_app.command("spawn-headless")(spawn_headless_command)
    loop_app.command("install-watchdog")(install_watchdog_command)
    loop_app.command("uninstall-watchdog")(uninstall_watchdog_command)
