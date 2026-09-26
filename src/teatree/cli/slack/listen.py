"""``t3 slack listen`` — run the Socket Mode receiver for Slack events."""

import logging
import traceback
from pathlib import Path
from typing import TYPE_CHECKING, cast

import typer

from teatree.backends.slack.receiver import default_queue_path, run_listener
from teatree.core.backend_factory import messaging_from_overlay
from teatree.core.on_behalf_egress import OnBehalfPostBlockedError, OnBehalfSlackEgress
from teatree.utils.django_bootstrap import ensure_django
from teatree.utils.secrets import read_pass
from teatree.utils.singleton import AlreadyRunningError, read_pid, singleton

if TYPE_CHECKING:
    from teatree.loop.scanners.slack_dm_inbound import SlackDmInboundScanner

slack_app = typer.Typer(name="slack", help="Slack integration commands.", no_args_is_help=True)

# 2, not 1: a crashing drain (Django boot failure, a DB error) also exits 1 with EMPTY
# stdout, byte-identical to the old empty-queue signal, so 1 is the crash signature and
# the quiet-box read had to move off it. NOTE: click also spends 2 on a usage error, so a
# malformed invocation reads as a healthy empty queue to the deploy watchdog — a real
# residual of this contract, tracked rather than silently repainted in a merge.
EMPTY_QUEUE_EXIT_CODE = 2
# 3: distinct from both the empty queue (2) and the crash signature (1), so a drain that
# could not run at all can never borrow a code the watchdog reads as healthy.
DRAIN_FAILED_EXIT_CODE = 3
DRAIN_FAILED_NOTICE = "ERROR slack drain could not run"


def _resolve_overlays(restrict: str) -> list[tuple[str, str, str]]:
    from teatree.config import cold_reader  # noqa: PLC0415 — deferred: keeps CLI startup light

    registry = cold_reader.mapping_setting("overlays")
    result: list[tuple[str, str, str]] = []
    for name, overlay_cfg in registry.items():
        if restrict and name != restrict:
            continue
        if not isinstance(overlay_cfg, dict):
            continue
        cfg = cast("dict[str, object]", overlay_cfg)
        if cfg.get("messaging_backend") != "slack":
            continue
        token_ref = cfg.get("slack_token_ref", "")
        if not token_ref:
            continue
        bot_token = read_pass(f"{token_ref}-bot")
        app_token = read_pass(f"{token_ref}-app")
        if bot_token and app_token:
            result.append((name, app_token, bot_token))
        else:
            typer.echo(f"WARN  {name}: missing bot or app token in pass at {token_ref}")
    return result


#: Per-overlay DM recorders, memoised for the listener process's lifetime so an
#: inbound DM does not re-probe the bot's own Slack identity on every event.
_dm_recorders: dict[str, "SlackDmInboundScanner"] = {}


def _dm_recorder(overlay: str) -> "SlackDmInboundScanner | None":
    """The recorder for *overlay*, or ``None`` when it has no messaging backend.

    An unresolvable backend is deliberately NOT memoised, so a transient
    resolution failure is re-probed on the next event instead of wedging the
    hot path for the process's lifetime.
    """
    from teatree.loop.scanners.slack_dm_inbound import SlackDmInboundScanner  # noqa: PLC0415 — deferred: ORM-backed

    recorder = _dm_recorders.get(overlay)
    if recorder is not None:
        return recorder
    backend = messaging_from_overlay(overlay or None)
    if backend is None:
        return None
    recorder = SlackDmInboundScanner(backend=backend, overlay=overlay)
    _dm_recorders[overlay] = recorder
    return recorder


def reset_dm_recorders() -> None:
    """Drop the per-overlay recorders. Test-only.

    A recorder holds the backend ``messaging_from_overlay`` resolved, plus the bot
    identity it probed through that backend — so keeping one outlives
    ``reset_backend_caches``, which conftest already runs, and hands a later test
    the earlier test's backend under the same overlay name.
    """
    _dm_recorders.clear()


def _record_and_wake_slack_answer(overlay: str, event: dict) -> None:
    """Record an inbound DM, then enqueue an immediate Slack-answer wake.

    Recording FIRST is what makes the wake worth firing: the answer cycle reads
    ``PendingChatInjection.loop_unreplied()``, whose only other writer is the
    ~60s inbox sweep, so a wake that lands before the row finds an empty queue
    and stops without re-arming. Recording goes through the sweep's own
    ``record_events`` seam, so the hot path inherits its write-side filters
    rather than reimplementing them.

    Nothing is posted to Slack here — every outbound reply stays in the answer
    cycle. Deferred imports: the receiver's ``backends`` layer cannot reach the
    orchestration layer, so the CLI composition root injects this.
    """
    from teatree.loops.timer_reconciler import wake_slack_answer  # noqa: PLC0415 — deferred import

    if event.get("type") == "message" and event.get("channel_type") == "im":
        recorder = _dm_recorder(overlay)
        if recorder is None:
            logging.getLogger(__name__).warning(
                "No slack backend for overlay %r — the inbox sweep will record this DM", overlay
            )
        else:
            recorder.record_events([event])
    wake_slack_answer.enqueue()


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
    file that the worker's mention scanner drains. Runs until SIGTERM or SIGINT.
    """
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)-5s %(name)s %(message)s",
    )
    # Bootstrap Django so the per-event wake callback can enqueue onto the task DB.
    ensure_django()
    pid_path = default_queue_path().with_name("slack-listener.pid")
    try:
        with singleton("slack-listener", pid_path=pid_path):
            overlays = _resolve_overlays(overlay)
            if not overlays:
                typer.echo("ERROR No slack-enabled overlays found in the DB overlays registry")
                raise typer.Exit(code=1)
            for name, _app, _bot in overlays:
                typer.echo(f"OK    Listening on {name}")
            run_listener(overlays, queue_path=queue_file, on_event=_record_and_wake_slack_answer)
    except AlreadyRunningError as exc:
        typer.echo(f"WARN  {exc} Stop it before starting another.")
        raise typer.Exit(code=1) from None


@slack_app.command("check")
def check_command() -> None:
    """Drain the event queue, ack with 👀, and print new user messages.

    Reads the JSONL queue written by ``t3 slack listen``, filters for
    user messages (ignoring bot posts), reacts with ``eyes`` on each
    to signal the bot has seen it, then prints each as a JSON line.
    Returns exit code 0 when messages were found, EMPTY_QUEUE_EXIT_CODE when
    the queue was empty. Designed to be called from a fast cron (every 30s).

    Exit code 2 (not 1) for the empty-queue case is deliberate: a crashing
    drain (Django boot failure, a DB error) also exits 1 with empty stdout —
    byte-identical to the old "empty queue" signal — so a caller polling on
    "rc=1 and no stdout" could not tell a healthy quiet box from a crash. 2
    is unambiguous, and preferred over an ``EMPTY`` stdout sentinel: stdout
    is already the message channel, and a sentinel would need filtering by
    every future consumer.

    A singleton guard serialises the drain: the 30s cron can double-fire and
    two concurrent drains would ack the same mentions twice, so a second drain
    stands down (exit 0) while the first holds the lock.

    A drain that could not run at all exits ``DRAIN_FAILED_EXIT_CODE`` and names
    its refusal on stderr. The deploy watchdog records the empty queue as healthy,
    so a failure that borrowed that exit code made the wedge invisible.
    """
    try:
        drain_pid = default_queue_path().with_name("slack-drain.pid")
        with singleton("slack-drain", pid_path=drain_pid):
            _drain_and_emit()
    except AlreadyRunningError:
        typer.echo("Another slack drain is in progress; standing down.", err=True)
        raise typer.Exit(code=0) from None
    except typer.Exit:
        raise
    except Exception as exc:  # noqa: BLE001 — the refusal must cover failures nobody enumerated.
        typer.echo(f"{DRAIN_FAILED_NOTICE}: {exc!r}\n{traceback.format_exc()}", err=True)
        raise typer.Exit(code=DRAIN_FAILED_EXIT_CODE) from None


def _drain_and_emit() -> None:
    """Drain the queue once, ack each user message, and emit them as JSON lines."""
    import json  # noqa: PLC0415 — deferred: loaded only when this command runs

    from teatree.backends.slack.receiver import commit_drain, drain_event_queue  # noqa: PLC0415 — lazy CLI import

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
        raise typer.Exit(code=EMPTY_QUEUE_EXIT_CODE)
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
        is blocked by the active posture (the message names the
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
