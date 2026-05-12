"""Socket Mode receiver — writes inbound Slack events to a JSONL queue file.

One global singleton process handles all slack-enabled overlays. Each overlay
gets its own WebSocket connection (one per ``xapp-`` token). Events are written
to a single append-only JSONL queue at ``$XDG_DATA_HOME/teatree/slack-events.jsonl``
tagged with the overlay name. The fat loop tick drains the queue via
:func:`drain_event_queue`.

Start with ``t3 slack listen`` (all overlays) or ``t3 slack listen --overlay X``.
"""

import json
import logging
import os
import signal
import threading
import time
from pathlib import Path

logger = logging.getLogger(__name__)

_QUEUE_FILENAME = "slack-events.jsonl"
_HANDLED_EVENT_TYPES = frozenset({"app_mention", "message"})


def default_queue_path() -> Path:
    xdg = os.environ.get("XDG_DATA_HOME")
    base = Path(xdg) if xdg else Path.home() / ".local" / "share"
    return base / "teatree" / _QUEUE_FILENAME


def _enqueue(path: Path, overlay: str, event: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    line = json.dumps({"overlay": overlay, "event": event}, separators=(",", ":"))
    with path.open("a", encoding="utf-8") as f:
        f.write(line + "\n")


def drain_event_queue(path: Path | None = None) -> list[dict]:
    path = path or default_queue_path()
    if not path.is_file():
        return []
    tmp = path.with_suffix(".draining")
    try:
        path.rename(tmp)
    except OSError:
        return []
    events = []
    for line in tmp.read_text(encoding="utf-8").strip().splitlines():
        try:
            events.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    tmp.unlink(missing_ok=True)
    return events


def _run_single_overlay(
    *,
    overlay_name: str,
    app_token: str,
    bot_token: str,
    queue_path: Path,
    stop_event: threading.Event,
) -> None:
    try:
        from slack_sdk.socket_mode import SocketModeClient  # noqa: PLC0415
        from slack_sdk.socket_mode.client import BaseSocketModeClient  # noqa: PLC0415
        from slack_sdk.socket_mode.request import SocketModeRequest  # noqa: PLC0415
        from slack_sdk.socket_mode.response import SocketModeResponse  # noqa: PLC0415
        from slack_sdk.web import WebClient  # noqa: PLC0415
    except ImportError:
        logger.warning("slack_sdk not installed — reinstall with: uv tool install --editable '.[slack]'")
        return

    web_client = WebClient(token=bot_token)
    client = SocketModeClient(app_token=app_token, web_client=web_client)

    def _handle(_sm_client: BaseSocketModeClient, req: SocketModeRequest) -> None:
        client.send_socket_mode_response(SocketModeResponse(envelope_id=req.envelope_id))
        if req.type != "events_api":
            return
        event = (req.payload or {}).get("event", {})
        event_type = event.get("type", "")
        if event_type not in _HANDLED_EVENT_TYPES:
            return
        subtype = event.get("subtype", "")
        if subtype in {"bot_message", "message_changed", "message_deleted"}:
            return
        _enqueue(queue_path, overlay_name, event)
        logger.info("[%s] Queued %s event (ts=%s)", overlay_name, event_type, event.get("ts", "?"))

    client.socket_mode_request_listeners.append(_handle)
    client.connect()
    logger.info("[%s] Socket Mode connected", overlay_name)

    while not stop_event.is_set():
        stop_event.wait(timeout=1.0)

    client.close()
    logger.info("[%s] Socket Mode disconnected", overlay_name)


def run_listener(
    overlays: list[tuple[str, str, str]],
    *,
    queue_path: Path | None = None,
) -> None:
    queue = queue_path or default_queue_path()
    stop = threading.Event()

    def _signal_handler(signum: int, frame: object) -> None:
        _ = signum, frame
        logger.info("Received signal, shutting down")
        stop.set()

    signal.signal(signal.SIGTERM, _signal_handler)
    signal.signal(signal.SIGINT, _signal_handler)

    threads: list[threading.Thread] = []
    for overlay_name, app_token, bot_token in overlays:
        t = threading.Thread(
            target=_run_single_overlay,
            kwargs={
                "overlay_name": overlay_name,
                "app_token": app_token,
                "bot_token": bot_token,
                "queue_path": queue,
                "stop_event": stop,
            },
            daemon=True,
            name=f"slack-{overlay_name}",
        )
        t.start()
        threads.append(t)
        logger.info("Started listener thread for %s", overlay_name)

    if not threads:
        logger.warning("No slack-enabled overlays found")
        return

    while not stop.is_set():
        time.sleep(1.0)

    for t in threads:
        t.join(timeout=5.0)
    logger.info("All listener threads stopped")
