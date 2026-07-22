"""Socket Mode receiver — writes inbound Slack events to JSONL queue files.

One global singleton process handles all slack-enabled overlays. Each overlay
gets its own WebSocket connection (one per ``xapp-`` token). Events are
partitioned across two append-only JSONL queues at
``$XDG_DATA_HOME/teatree/`` so independent scanners can each own a queue
without racing on a shared drain:

* ``slack-events.jsonl`` — ``app_mention`` and ``message`` events
    (consumed by :class:`SlackMentionsScanner`).
* ``slack-reactions.jsonl`` — ``reaction_added`` events
    (consumed by :class:`SlackReviewIntentScanner`, #1047).

Start with ``t3 slack listen`` (all overlays) or ``t3 slack listen --overlay X``.
"""

import json
import logging
import os
import signal
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

logger = logging.getLogger(__name__)

_QUEUE_FILENAME = "slack-events.jsonl"
_REACTIONS_QUEUE_FILENAME = "slack-reactions.jsonl"
_HANDLED_EVENT_TYPES = frozenset({"app_mention", "message", "reaction_added"})


def _data_dir() -> Path:
    xdg = os.environ.get("XDG_DATA_HOME")
    base = Path(xdg) if xdg else Path.home() / ".local" / "share"
    return base / "teatree"


def default_queue_path() -> Path:
    return _data_dir() / _QUEUE_FILENAME


def default_reactions_queue_path() -> Path:
    return _data_dir() / _REACTIONS_QUEUE_FILENAME


@dataclass(frozen=True, slots=True)
class QueuePaths:
    """Per-listener JSONL queue file locations.

    Mentions and messages share one file; reactions get a dedicated file so
    :class:`SlackMentionsScanner` and :class:`SlackReviewIntentScanner` each
    own an atomic-rename drain without racing on a shared inode (#1047).
    """

    events: Path
    reactions: Path

    def for_event_type(self, event_type: str) -> Path:
        if event_type == "reaction_added":
            return self.reactions
        return self.events


def _enqueue(path: Path, overlay: str, event: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    line = json.dumps({"overlay": overlay, "event": event}, separators=(",", ":"))
    with path.open("a", encoding="utf-8") as f:
        f.write(line + "\n")


def drain_event_queue(path: Path | None = None) -> list[dict]:
    """Read queued events into memory using recover-then-drain ordering.

    A previous drain that crashed before its caller durably persisted leaves
    its ``.draining`` file on disk; this drain recovers those events first,
    then folds in any newly enqueued live events. The ``.draining`` file is
    left in place — the caller unlinks it via :func:`commit_drain` only after
    the durable persist succeeds. A crash in the window between this return
    and the persist therefore loses nothing: the next drain recovers it.
    Slack never retries ``app_mention`` delivery, so this ordering is the
    only thing preventing permanent mention loss.
    """
    path = path or default_queue_path()
    tmp = path.with_suffix(".draining")
    if not tmp.exists():
        # No leftover from a crashed drain: claim the live queue atomically.
        # If there is no live file either, there is nothing to drain.
        if not path.is_file():
            return []
        try:
            path.rename(tmp)
        except OSError:
            return []
    # A leftover ``.draining`` is drained as-is; the live file (if any) is
    # left untouched and claimed on the next drain after ``commit_drain``.
    # Recovery therefore stays a pure atomic rename — no live-file copy that
    # could drop a concurrent ``_enqueue``.
    if not tmp.is_file():
        return []
    events = []
    for line in tmp.read_text(encoding="utf-8").strip().splitlines():
        try:
            events.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    return events


def commit_drain(path: Path | None = None) -> None:
    """Discard the drained backing file after the caller durably persisted.

    Call this only once the events returned by :func:`drain_event_queue` are
    safely persisted; until then the ``.draining`` file must stay so a crash
    is recoverable.
    """
    path = path or default_queue_path()
    path.with_suffix(".draining").unlink(missing_ok=True)


def drain_reactions_queue(path: Path | None = None) -> list[dict]:
    """Drain the reactions JSONL queue (#1047).

    Mirrors :func:`drain_event_queue` but reads ``slack-reactions.jsonl``.
    Kept as a separate function (rather than a parameter) so callers can't
    accidentally drain the wrong queue, and so the file system race
    (atomic rename) stays scoped per file.
    """
    return drain_event_queue(path or default_reactions_queue_path())


def commit_reactions_drain(path: Path | None = None) -> None:
    """Discard the drained reactions backing file after a durable persist.

    Reactions counterpart of :func:`commit_drain` (#1047).
    """
    commit_drain(path or default_reactions_queue_path())


def _run_single_overlay(
    *,
    overlay: tuple[str, str, str],
    queues: QueuePaths,
    stop_event: threading.Event,
    on_event: Callable[[], None] | None = None,
) -> None:
    overlay_name, app_token, bot_token = overlay
    try:
        from slack_sdk.socket_mode import SocketModeClient  # noqa: PLC0415 — deferred: heavy/optional dep at call site
        from slack_sdk.socket_mode.client import BaseSocketModeClient  # noqa: PLC0415 — deferred: heavy/optional dep
        from slack_sdk.socket_mode.request import SocketModeRequest  # noqa: PLC0415 — deferred: heavy/optional dep
        from slack_sdk.socket_mode.response import SocketModeResponse  # noqa: PLC0415 — deferred: heavy/optional dep
        from slack_sdk.web import WebClient  # noqa: PLC0415 — deferred: heavy/optional dep at call site
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
        target = queues.for_event_type(event_type)
        _enqueue(target, overlay_name, event)
        logger.info("[%s] Queued %s event (ts=%s)", overlay_name, event_type, event.get("ts", "?"))
        if on_event is not None:
            # Best-effort event-driven wake so the answer cycle runs now instead of
            # at the next cadence tick. The JSONL write above is the durable buffer,
            # so a failed signal only costs latency — never an event.
            try:
                on_event()
            except Exception:
                logger.warning("[%s] slack-answer wake signal failed", overlay_name, exc_info=True)

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
    reactions_queue_path: Path | None = None,
    on_event: Callable[[], None] | None = None,
) -> None:
    queues = QueuePaths(
        events=queue_path or default_queue_path(),
        reactions=reactions_queue_path or default_reactions_queue_path(),
    )
    stop = threading.Event()

    def _signal_handler(signum: int, frame: object) -> None:
        _ = signum, frame
        logger.info("Received signal, shutting down")
        stop.set()

    signal.signal(signal.SIGTERM, _signal_handler)
    signal.signal(signal.SIGINT, _signal_handler)

    threads: list[threading.Thread] = []
    for overlay in overlays:
        t = threading.Thread(
            target=_run_single_overlay,
            kwargs={
                "overlay": overlay,
                "queues": queues,
                "stop_event": stop,
                "on_event": on_event,
            },
            daemon=True,
            name=f"slack-{overlay[0]}",
        )
        t.start()
        threads.append(t)
        logger.info("Started listener thread for %s", overlay[0])

    if not threads:
        logger.warning("No slack-enabled overlays found")
        return

    while not stop.is_set():
        time.sleep(1.0)

    for t in threads:
        t.join(timeout=5.0)
    logger.info("All listener threads stopped")
