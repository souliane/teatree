"""``t3 slack listen`` — run the Socket Mode receiver for Slack events."""

import logging
import os
from pathlib import Path

import typer

from teatree.backends.slack_receiver import default_queue_path, run_listener
from teatree.utils.secrets import read_pass

slack_app = typer.Typer(name="slack", help="Slack integration commands.", no_args_is_help=True)


def _resolve_overlays(restrict: str) -> list[tuple[str, str, str]]:
    import tomllib  # noqa: PLC0415

    config_path = Path.home() / ".teatree.toml"
    if not config_path.is_file():
        return []
    with config_path.open("rb") as f:
        config = tomllib.load(f)
    result: list[tuple[str, str, str]] = []
    for name, overlay_cfg in (config.get("overlays") or {}).items():
        if restrict and name != restrict:
            continue
        if overlay_cfg.get("messaging_backend") != "slack":
            continue
        token_ref = overlay_cfg.get("slack_token_ref", "")
        if not token_ref:
            continue
        bot_token = read_pass(f"{token_ref}-bot")
        app_token = read_pass(f"{token_ref}-app")
        if bot_token and app_token:
            result.append((name, app_token, bot_token))
        else:
            typer.echo(f"WARN  {name}: missing bot or app token in pass at {token_ref}")
    return result


@slack_app.command("listen")
def listen_command(
    *,
    overlay: str = typer.Option("", "--overlay", help="Restrict to a single overlay (default: all)."),
    queue_file: Path = typer.Option(
        None,
        "--queue-file",
        help="Override the event queue path (test hook).",
    ),
) -> None:
    """Run the Socket Mode receiver for all (or one) slack-enabled overlays.

    Maintains one WebSocket per overlay, writes events to a JSONL queue
    file that the fat loop tick drains. Runs until SIGTERM or SIGINT.
    """
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)-5s %(name)s %(message)s",
    )
    pid_path = default_queue_path().with_name("slack-listener.pid")
    pid_path.parent.mkdir(parents=True, exist_ok=True)
    if pid_path.is_file():
        old_pid = pid_path.read_text(encoding="utf-8").strip()
        if old_pid.isdigit() and _pid_alive(int(old_pid)):
            typer.echo(f"WARN  Listener already running (PID {old_pid}). Use --overlay for a second instance.")
            raise typer.Exit(code=1)
    pid_path.write_text(str(os.getpid()) + "\n", encoding="utf-8")

    overlays = _resolve_overlays(overlay)
    if not overlays:
        typer.echo("ERROR No slack-enabled overlays found in ~/.teatree.toml")
        pid_path.unlink(missing_ok=True)
        raise typer.Exit(code=1)

    for name, _app, _bot in overlays:
        typer.echo(f"OK    Listening on {name}")

    try:
        run_listener(overlays, queue_path=queue_file)
    finally:
        pid_path.unlink(missing_ok=True)


@slack_app.command("check")
def check_command() -> None:
    """Drain the event queue and print new user messages.

    Reads the JSONL queue written by ``t3 slack listen``, filters for
    user messages (ignoring bot posts), and prints each as a line.
    Returns exit code 0 when messages were found, 1 when the queue
    was empty. Designed to be called from a fast cron (every 30s).
    """
    import json  # noqa: PLC0415

    from teatree.backends.slack_receiver import drain_event_queue  # noqa: PLC0415

    events = drain_event_queue()
    messages: list[dict[str, str]] = []
    for entry in events:
        event = entry.get("event", {})
        if not isinstance(event, dict):
            continue
        if event.get("bot_id") or event.get("subtype"):
            continue
        text = event.get("text", "")
        user = event.get("user", "")
        overlay = entry.get("overlay", "")
        if text and user:
            messages.append({"overlay": overlay, "user": user, "text": text, "ts": event.get("ts", "")})
    if not messages:
        raise typer.Exit(code=1)
    for msg in messages:
        typer.echo(json.dumps(msg))


@slack_app.command("status")
def status_command() -> None:
    """Check if the Socket Mode listener is running."""
    pid_path = default_queue_path().with_name("slack-listener.pid")
    if not pid_path.is_file():
        typer.echo("Listener: not running (no PID file)")
        raise typer.Exit(code=1)
    pid_str = pid_path.read_text(encoding="utf-8").strip()
    if not pid_str.isdigit():
        typer.echo("Listener: stale PID file")
        pid_path.unlink(missing_ok=True)
        raise typer.Exit(code=1)
    pid = int(pid_str)
    if _pid_alive(pid):
        typer.echo(f"Listener: running (PID {pid})")
    else:
        typer.echo(f"Listener: dead (PID {pid} not found)")
        pid_path.unlink(missing_ok=True)
        raise typer.Exit(code=1)


def _pid_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True
