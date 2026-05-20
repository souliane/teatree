"""``t3 slack listen`` — run the Socket Mode receiver for Slack events."""

import logging
from pathlib import Path

import httpx
import typer

from teatree.backends.slack_receiver import default_queue_path, run_listener
from teatree.cli.slack_user_token_setup import USER_TOKEN_PASS_KEY
from teatree.utils.secrets import read_pass
from teatree.utils.singleton import AlreadyRunningError, read_pid, singleton

slack_app = typer.Typer(name="slack", help="Slack integration commands.", no_args_is_help=True)


def _resolve_reaction_token() -> str:
    """Return the personal Slack user-OAuth token (``xoxp-…``) for reactions.

    Reactions on user DMs and on Slack-Connect externally-shared channels
    are rejected when sent with the bot token (``message_not_found`` and
    ``mcp_externally_shared_channel_restricted`` respectively — see
    BLUEPRINT § "Slack token routing" and ``feedback_slack_reactions_via_
    personal_token_not_bot``). The personal ``xoxp-…`` token provisioned
    by ``t3 setup slack-user-token`` (#1210/#1220/#1232) is the only
    credential that reliably reaches both surfaces; this helper centralises
    the lookup so every ad-hoc reaction call site (``t3 slack react`` and
    the ``t3 slack check`` ack path) shares a single source of truth.
    """
    return read_pass(USER_TOKEN_PASS_KEY)


def post_reaction(*, token: str, channel: str, ts: str, emoji: str) -> bool:
    """Call Slack ``reactions.add`` with *token* and return success.

    Treats ``already_reacted`` as success — the desired end state is the
    emoji being present on the message. Network and API failures are
    logged but never raised: a Slack outage must not break a fast loop
    tick. Mirrors :func:`teatree.backends.slack_reactions.add_reaction`
    but kept inline here so the CLI surface does not pull the FSM-side
    transition-reaction module just for an HTTP POST.
    """
    if not (token and channel and ts and emoji):
        return False
    try:
        response = httpx.post(
            "https://slack.com/api/reactions.add",
            headers={"Authorization": f"Bearer {token}"},
            json={"channel": channel, "timestamp": ts, "name": emoji},
            timeout=10.0,
        )
    except httpx.HTTPError as exc:
        logging.getLogger(__name__).warning("Slack reactions.add transport failed: %s", exc)
        return False
    if not response.is_success:
        logging.getLogger(__name__).warning("Slack reactions.add HTTP %s", response.status_code)
        return False
    payload = response.json()
    if payload.get("ok"):
        return True
    error = payload.get("error", "")
    if error == "already_reacted":
        return True
    logging.getLogger(__name__).warning("Slack reactions.add error: %s", error)
    return False


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
            channel = event.get("channel", "")
            ts = event.get("ts", "")
            messages.append({"overlay": overlay, "user": user, "text": text, "ts": ts, "channel": channel})
    if not messages:
        raise typer.Exit(code=1)
    _ack_messages(messages)
    for msg in messages:
        typer.echo(json.dumps(msg))


def _ack_messages(messages: list[dict[str, str]]) -> None:
    """React with 👀 on each message via the personal user OAuth token.

    User DMs and Slack-Connect channels reject the bot token for
    ``reactions.add`` (``message_not_found`` / ``mcp_externally_shared_
    channel_restricted``); routing through the personal ``xoxp-…`` token
    at ``pass slack/user-oauth-token`` is the only path that reliably
    succeeds on both surfaces (#1232).
    """
    token = _resolve_reaction_token()
    if not token:
        logging.getLogger(__name__).warning(
            "Personal Slack user-OAuth token missing at `pass %s` — "
            "run `t3 setup slack-user-token` to enable reaction acks.",
            USER_TOKEN_PASS_KEY,
        )
        return
    for msg in messages:
        channel = msg.get("channel", "")
        ts = msg.get("ts", "")
        post_reaction(token=token, channel=channel, ts=ts, emoji="eyes")


@slack_app.command("react")
def react_command(
    channel: str = typer.Argument(..., help="Slack channel id (e.g. `D…` for a DM, `C…` for a channel)."),
    ts: str = typer.Argument(..., help="Message timestamp (e.g. `1700000000.000100`)."),
    emoji: str = typer.Argument(..., help="Emoji name without colons (e.g. `eyes`, `white_check_mark`)."),
) -> None:
    """Add *emoji* to ``(channel, ts)`` using the personal user-OAuth token.

    The personal ``xoxp-…`` token at ``pass slack/user-oauth-token``
    (provisioned by ``t3 setup slack-user-token``) is the only credential
    that reliably reaches user DMs and Slack-Connect externally-shared
    channels for ``reactions.add`` (#1232). Exits 0 on success (including
    the idempotent ``already_reacted`` case), 1 when the token is missing,
    2 on any other Slack-side failure.
    """
    token = _resolve_reaction_token()
    if not token:
        typer.echo(
            f"ERROR Personal Slack user-OAuth token missing at `pass {USER_TOKEN_PASS_KEY}`. "
            "Run `t3 setup slack-user-token` first."
        )
        raise typer.Exit(code=1)
    if post_reaction(token=token, channel=channel, ts=ts, emoji=emoji):
        typer.echo(f"OK    Reacted :{emoji}: on {channel}/{ts}")
        return
    typer.echo(f"ERROR reactions.add failed for {channel}/{ts}; see logs.")
    raise typer.Exit(code=2)


@slack_app.command("status")
def status_command() -> None:
    """Check if the Socket Mode listener is running."""
    pid_path = default_queue_path().with_name("slack-listener.pid")
    pid = read_pid(pid_path)
    if pid is None:
        typer.echo("Listener: not running")
        raise typer.Exit(code=1)
    typer.echo(f"Listener: running (PID {pid})")
