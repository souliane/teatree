"""``t3 slack listen`` — run the Socket Mode receiver for Slack events."""

import logging
from pathlib import Path

import typer

from teatree.backends.slack.receiver import default_queue_path, run_listener
from teatree.core.backend_factory import messaging_from_overlay
from teatree.core.on_behalf_egress import OnBehalfPostBlockedError, OnBehalfSlackEgress
from teatree.utils.django_bootstrap import ensure_django
from teatree.utils.secrets import read_pass
from teatree.utils.singleton import AlreadyRunningError, read_pid, singleton

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
    file that the drain-queue loop drains. Runs until SIGTERM or SIGINT.
    """
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)-5s %(name)s %(message)s",
    )
    pid_path = default_queue_path().with_name("slack-listener.pid")
    try:
        with singleton("slack-listener", pid_path=pid_path):
            overlays = _resolve_overlays(overlay)
            if not overlays:
                typer.echo("ERROR No slack-enabled overlays found in ~/.teatree.toml")
                raise typer.Exit(code=1)
            for name, _app, _bot in overlays:
                typer.echo(f"OK    Listening on {name}")
            run_listener(overlays, queue_path=queue_file)
    except AlreadyRunningError as exc:
        typer.echo(f"WARN  {exc}. Stop it before starting another.")
        raise typer.Exit(code=1) from None


@slack_app.command("check")
def check_command() -> None:
    """Drain the event queue, ack with 👀, and print new user messages.

    Reads the JSONL queue written by ``t3 slack listen``, filters for
    user messages (ignoring bot posts), reacts with ``eyes`` on each
    to signal the bot has seen it, then prints each as a JSON line.
    Returns exit code 0 when messages were found, 1 when the queue
    was empty. Designed to be called from a fast cron (every 30s).
    """
    import json  # noqa: PLC0415

    from teatree.backends.slack.receiver import commit_drain, drain_event_queue  # noqa: PLC0415

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
            channel = event.get("channel", "")
            ts = event.get("ts", "")
            messages.append({"overlay": overlay, "user": user, "text": text, "ts": ts, "channel": channel})
    if not messages:
        # Nothing actionable to emit, but the drained file must still be
        # discarded so empty/bot-only events don't replay on every drain.
        commit_drain()
        raise typer.Exit(code=1)
    _ack_messages(messages)
    # Discard the backing file only after acking — a crash before this point
    # leaves it for the next drain to recover (Slack never retries mentions).
    commit_drain()
    for msg in messages:
        typer.echo(json.dumps(msg))


def _ack_messages(messages: list[dict[str, str]]) -> None:
    """React with 👀 on each inbound user message through the on-behalf egress.

    Each drained message is the user's own inbound DM/mention, so the
    egress classifies it self-DM and the :eyes: ack stays ungated (the same
    #1750 ``route_token`` self branch). Routing it through the one egress
    keeps every reaction under one classifier instead of a raw personal-xoxp
    ``reactions.add``. A reaction that fails (transport, auth gap, or a
    colleague-channel mention now correctly gated) is logged and skipped —
    the fast cron must not break the whole tick on one ack.
    """
    ensure_django()
    for msg in messages:
        overlay = msg.get("overlay", "")
        channel = msg.get("channel", "")
        ts = msg.get("ts", "")
        backend = messaging_from_overlay(overlay or None)
        if backend is None:
            logging.getLogger(__name__).warning("No slack backend for overlay %r — skipping :eyes: ack", overlay)
            continue
        try:
            OnBehalfSlackEgress(backend).react(
                channel=channel,
                ts=ts,
                emoji="eyes",
                target=channel,
                action="slack_check_ack",
                destination=channel,
            )
        except OnBehalfPostBlockedError as blocked:
            logging.getLogger(__name__).warning("Skipping gated :eyes: ack on %s/%s: %s", channel, ts, blocked)
        except Exception as exc:  # noqa: BLE001 — a single ack failure must not break the cron tick.
            logging.getLogger(__name__).warning("Skipping :eyes: ack on %s/%s: %s", channel, ts, exc)


@slack_app.command("react")
def react_command(
    channel: str = typer.Argument(..., help="Slack channel id (e.g. `D…` for a DM, `C…` for a channel)."),
    ts: str = typer.Argument(..., help="Message timestamp (e.g. `1700000000.000100`)."),
    emoji: str = typer.Argument(..., help="Emoji name without colons (e.g. `eyes`, `white_check_mark`)."),
    overlay: str = typer.Option("", "--overlay", help="Overlay whose Slack credentials route the reaction."),
) -> None:
    """Add *emoji* to ``(channel, ts)`` through the on-behalf egress (#960/#1750).

    Routes through :class:`OnBehalfSlackEgress` on the route-aware backend:
    a reaction on the user's own DM stays ungated, a reaction on a colleague
    or channel message is gated+audited under the on-behalf discipline. The
    backend resolves from ``--overlay`` or ``T3_OVERLAY_NAME``.

    Exit codes:

    - ``0`` — success (including the idempotent ``already_reacted`` case).
    - ``1`` — no slack backend resolvable, OR the colleague-surface reaction
        is blocked by ``on_behalf_post_mode`` (the message names the
        ``t3 review approve-on-behalf`` satisfier), OR Slack rejected the
        call (``missing_scope``, ``not_in_channel``, …).
    """
    ensure_django()
    backend = messaging_from_overlay(overlay or None)
    if backend is None:
        typer.echo("ERROR No slack backend resolvable — set --overlay or T3_OVERLAY_NAME.")
        raise typer.Exit(code=1)
    try:
        response = OnBehalfSlackEgress(backend).react(
            channel=channel,
            ts=ts,
            emoji=emoji,
            target=channel,
            action="adhoc_slack_react",
            destination=channel,
        )
    except OnBehalfPostBlockedError as blocked:
        typer.echo(f"ERROR {blocked}")
        raise typer.Exit(code=1) from blocked
    error = str(response.get("error") or "")
    if response.get("ok") or error == "already_reacted":
        typer.echo(f"OK    Reacted :{emoji}: on {channel}/{ts}")
        return
    typer.echo(f"ERROR reactions.add failed for {channel}/{ts}: {error or 'unknown_error'}")
    raise typer.Exit(code=1)


@slack_app.command("status")
def status_command() -> None:
    """Check if the Socket Mode listener is running."""
    pid_path = default_queue_path().with_name("slack-listener.pid")
    pid = read_pid(pid_path)
    if pid is None:
        typer.echo("Listener: not running")
        raise typer.Exit(code=1)
    typer.echo(f"Listener: running (PID {pid})")
