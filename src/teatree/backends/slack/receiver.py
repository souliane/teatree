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

A tap on a Block Kit button arrives as an ``interactive`` envelope; it is acknowledged and
handed to the ``on_action`` callback, never queued (#4990).

Start with ``t3 slack listen`` (all overlays) or ``t3 slack listen --overlay X``.
Liveness and the catch-up a ``hello`` owes live in
:mod:`teatree.backends.slack.listener_heartbeat`.
"""

import json
import logging
import os
import signal
import threading
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Protocol, cast

from teatree.backends.slack.listener_heartbeat import (
    HEARTBEAT_INTERVAL_SECONDS,
    ConnectionLiveness,
    OnConnect,
    heartbeat_path,
    read_heartbeat,
    write_heartbeat,
)
from teatree.backends.slack.self_identity import (
    OwnSlackIdentity,
    identity_from_auth_test,
    is_self_authored,
    is_self_originated,
)

if TYPE_CHECKING:
    from teatree.types import RawAPIDict

from teatree.utils.uv_constraints import uv_tool_install_hint

logger = logging.getLogger(__name__)

_QUEUE_FILENAME = "slack-events.jsonl"
_REACTIONS_QUEUE_FILENAME = "slack-reactions.jsonl"
_SERVE_TICK_SECONDS = 1.0
_HANDLED_EVENT_TYPES = frozenset({"app_mention", "message", "reaction_added"})

#: Called with ``(overlay_name, event)`` right after an event is durably queued.
type OnEvent = Callable[[str, dict], None]

#: Called with ``(overlay_name, payload)`` for each button tap, after the envelope is acknowledged.
type OnAction = Callable[[str, dict], None]

_TAP_PAYLOAD_TYPE = "block_actions"


@dataclass(slots=True)
class DeliveredEvents:
    events: dict[str, float] = field(default_factory=dict)
    _on_delivery: Callable[[dict[str, float]], None] | None = field(default=None, init=False, repr=False)
    _lock: threading.Lock = field(default_factory=threading.Lock, init=False, repr=False)

    @staticmethod
    def _key(event: dict) -> tuple[str, float] | None:
        kind = event.get("type")
        if kind == "reaction_added":
            item = event.get("item")
            if isinstance(item, dict):
                channel, stamp = item.get("channel"), item.get("ts")
                reaction, user = event.get("reaction"), event.get("user")
                if all(isinstance(value, str) and value for value in (channel, stamp, reaction, user)):
                    return f"reaction:{channel}:{stamp}:{reaction}:{user}", _event_seconds(stamp)
            return None
        if kind not in {"message", "app_mention"}:
            return None
        channel, stamp = event.get("channel"), event.get("ts")
        if not isinstance(channel, str) or not channel or not isinstance(stamp, str) or not stamp:
            return None
        return f"{kind}:{channel}:{stamp}", _event_seconds(stamp)

    def deliver_once(self, event: dict, deliver: Callable[[], bool]) -> bool:
        pair = self._key(event)
        if pair is None:
            return deliver()
        key, stamp = pair
        with self._lock:
            if key in self.events:
                return False
            delivered = deliver()
            if delivered:
                self.events[key] = stamp
                if self._on_delivery is not None:
                    floor = time.time() - 9 * 86400
                    self.events = {item: at for item, at in self.events.items() if at >= floor}
                    self._on_delivery(dict(self.events))
            return delivered

    def on_delivery(self, callback: Callable[[dict[str, float]], None]) -> None:
        with self._lock:
            self._on_delivery = callback

    def prune(self, *, before: float) -> None:
        with self._lock:
            self.events = {key: stamp for key, stamp in self.events.items() if stamp >= before}

    def update(self, values: Mapping[str, float]) -> None:
        with self._lock:
            self.events.update(values)

    def as_mapping(self) -> dict[str, float]:
        with self._lock:
            return dict(self.events)

    @classmethod
    def from_mapping(cls, values: Mapping[str, float]) -> "DeliveredEvents":
        return cls(events=dict(values))


def _event_seconds(value: str) -> float:
    try:
        return float(value)
    except ValueError:
        return 0.0


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
    heartbeat: Path | None = None

    @classmethod
    def default(cls) -> "QueuePaths":
        return cls(events=default_queue_path(), reactions=default_reactions_queue_path(), heartbeat=heartbeat_path())

    def for_event_type(self, event_type: str) -> Path:
        if event_type == "reaction_added":
            return self.reactions
        return self.events


@dataclass(frozen=True, slots=True)
class DeliveryContext:
    queues: QueuePaths
    identity: OwnSlackIdentity | None
    on_event: OnEvent | None
    seen: DeliveredEvents


def _enqueue(path: Path, overlay: str, event: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    line = json.dumps({"overlay": overlay, "event": event}, separators=(",", ":"))
    with path.open("a", encoding="utf-8") as f:
        f.write(line + "\n")


def record_is_owned_by(record: dict, overlay: str) -> bool:
    """Whether a queued record belongs to *overlay*'s consumer.

    One JSONL file carries every overlay's events, so a multi-overlay tick has
    several consumers racing the same drain; the ``overlay`` key each record
    carries is what tells them apart. A consumer with no overlay (the
    single-overlay wiring) and an untagged record both fall through to owned,
    so the partition narrows nothing outside a genuine multi-overlay install.
    """
    if not overlay:
        return True
    queued_overlay = record.get("overlay")
    return not queued_overlay or queued_overlay == overlay


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


def commit_drain(path: Path | None = None, *, retain: list[dict] | None = None) -> None:
    """Discard the drained backing file after the caller durably persisted.

    Call this only once the events returned by :func:`drain_event_queue` are
    safely persisted; until then the ``.draining`` file must stay so a crash
    is recoverable.

    *retain* holds the records this consumer did NOT handle — a sibling
    overlay's, or one whose handler raised. They are appended back to the LIVE
    queue rather than left in ``.draining``, because a leftover ``.draining``
    blocks every later drain from claiming the live file.
    """
    path = path or default_queue_path()
    if retain:
        _requeue(path, retain)
    path.with_suffix(".draining").unlink(missing_ok=True)


def _requeue(path: Path, records: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as f:
        for record in records:
            f.write(json.dumps(record, separators=(",", ":")) + "\n")


def drain_reactions_queue(path: Path | None = None) -> list[dict]:
    """Drain the reactions JSONL queue (#1047).

    Mirrors :func:`drain_event_queue` but reads ``slack-reactions.jsonl``.
    Kept as a separate function (rather than a parameter) so callers can't
    accidentally drain the wrong queue, and so the file system race
    (atomic rename) stays scoped per file.
    """
    return drain_event_queue(path or default_reactions_queue_path())


def commit_reactions_drain(path: Path | None = None, *, retain: list[dict] | None = None) -> None:
    """Discard the drained reactions backing file after a durable persist.

    Reactions counterpart of :func:`commit_drain` (#1047).
    """
    commit_drain(path or default_reactions_queue_path(), retain=retain)


def deliver_event(
    overlay: str,
    event: dict,
    *,
    context: DeliveryContext,
) -> bool:
    """Queue one Slack event and fire *on_event* for it — the live and the replayed path alike.

    ``False`` when the event type or subtype is not one the receiver handles.
    """
    event_type = event.get("type", "")
    if event_type not in _HANDLED_EVENT_TYPES:
        return False
    subtype = event.get("subtype", "")
    if subtype in {"bot_message", "message_changed", "message_deleted"}:
        return False

    def enqueue() -> bool:
        target = context.queues.for_event_type(event_type)
        _enqueue(target, overlay, event)
        return True

    if not context.seen.deliver_once(event, enqueue):
        return False
    logger.info("[%s] Queued %s event (ts=%s)", overlay, event_type, event.get("ts", "?"))
    if context.on_event is None:
        return True
    if is_self_originated(event, context.identity) and not _may_be_user_token_thread_reply(event, context.identity):
        logger.debug("[%s] no wake: self-originated event (ts=%s)", overlay, event.get("ts", "?"))
        return True
    # Best-effort event-driven record-then-wake so the answer cycle finds the
    # row it reads instead of waking on an empty queue. The JSONL write above
    # is the durable buffer, so a raising callback only costs latency — the
    # inbox drain still records the event.
    try:
        context.on_event(overlay, event)
    except Exception:
        logger.warning("[%s] slack-answer wake signal failed", overlay, exc_info=True)
    return True


def _may_be_user_token_thread_reply(event: dict, identity: OwnSlackIdentity | None) -> bool:
    user = event.get("user")
    thread_ts = event.get("thread_ts")
    return (
        event.get("channel_type") == "im"
        and isinstance(user, str)
        and bool(user)
        and isinstance(thread_ts, str)
        and bool(thread_ts)
        and thread_ts != event.get("ts")
        and not event.get("bot_id")
        and (identity is None or not is_self_authored(event, identity))
    )


class _LiveSocket(Protocol):
    def is_connected(self) -> bool: ...

    def session_id(self) -> str | None: ...


def _catch_up_once(
    overlay: str, alive_at: float, on_connect: OnConnect, liveness: ConnectionLiveness, seen: DeliveredEvents
) -> None:
    started_at = time.time()
    try:
        on_connect(overlay, alive_at, seen)
    except Exception:
        logger.warning("[%s] Slack catch-up failed; retrying with backoff", overlay, exc_info=True)
        liveness.failed(time.time())
        return
    liveness.succeeded(started_at)


def _serve_until_stopped(
    *,
    overlay: str,
    stop_event: threading.Event,
    client: _LiveSocket,
    liveness: ConnectionLiveness,
    seen: DeliveredEvents | None = None,
) -> None:
    """Idle until shutdown: run the catch-up a hello owes, then restamp the heartbeat on its cadence.

    Reached only past ``client.connect()``, which is what lets the stamp attest to a
    live socket rather than to a process that is merely still running.
    """
    next_beat = 0.0
    seen = seen or DeliveredEvents()
    while True:
        on_connect = liveness.on_connect
        if on_connect is not None and (alive_at := liveness.catch_up_due(time.time())) is not None:
            _catch_up_once(overlay, alive_at, on_connect, liveness, seen)
        if time.monotonic() >= next_beat and client.is_connected() and liveness.may_stamp(client.session_id()):
            liveness.stamped(write_heartbeat(overlay, liveness.heartbeat_path))
            next_beat = time.monotonic() + HEARTBEAT_INTERVAL_SECONDS
        if stop_event.wait(timeout=_SERVE_TICK_SECONDS):
            return


class _AuthTestClient(Protocol):
    """The one ``WebClient`` method the wake self-filter needs."""

    def auth_test(self) -> object: ...


def _probe_own_identity(web_client: _AuthTestClient, overlay_name: str) -> OwnSlackIdentity | None:
    """The bot's own ids, probed ONCE per connection — ``None`` when unavailable.

    Per connection rather than per event: probing per message is what made
    ``auth.test`` hot enough to be rate-limited (#4707). An unresolved identity
    costs precision, not the guard — :func:`is_self_originated` still reads
    ``bot_id`` and ``api_app_id`` straight off the event.
    """
    try:
        response = web_client.auth_test()
    except Exception:  # noqa: BLE001 — a probe failure degrades the filter, never the listener
        logger.warning("[%s] auth.test raised; wake self-filter runs on event fields alone", overlay_name)
        return None
    body = getattr(response, "data", response)
    identity = identity_from_auth_test(cast("RawAPIDict", body) if isinstance(body, dict) else None)
    if identity is None:
        logger.warning(
            "[%s] auth.test gave no usable identity; wake self-filter runs on event fields alone", overlay_name
        )
    else:
        logger.info("[%s] own identity user=%s bot=%s", overlay_name, identity.user_id, identity.bot_id)
    return identity


def _hand_over_tap(overlay_name: str, payload: dict, on_action: OnAction | None) -> None:
    if on_action is None or payload.get("type") != _TAP_PAYLOAD_TYPE:
        return
    try:
        on_action(overlay_name, payload)
    except Exception:
        logger.warning("[%s] slack click handler failed", overlay_name, exc_info=True)


@dataclass(frozen=True, slots=True)
class ListenerCallbacks:
    on_event: OnEvent | None = None
    on_action: OnAction | None = None


def _run_single_overlay(
    *,
    overlay: tuple[str, str, str],
    queues: QueuePaths,
    stop_event: threading.Event,
    callbacks: ListenerCallbacks | None = None,
    liveness: ConnectionLiveness | None = None,
) -> None:
    overlay_name, app_token, bot_token = overlay
    callbacks = callbacks or ListenerCallbacks()
    liveness = liveness or ConnectionLiveness()
    seen = DeliveredEvents()
    try:
        from slack_sdk.socket_mode import SocketModeClient  # noqa: PLC0415 — deferred: heavy/optional dep at call site
        from slack_sdk.socket_mode.client import BaseSocketModeClient  # noqa: PLC0415 — deferred: heavy/optional dep
        from slack_sdk.socket_mode.request import SocketModeRequest  # noqa: PLC0415 — deferred: heavy/optional dep
        from slack_sdk.socket_mode.response import SocketModeResponse  # noqa: PLC0415 — deferred: heavy/optional dep
        from slack_sdk.web import WebClient  # noqa: PLC0415 — deferred: heavy/optional dep at call site
    except ImportError:
        repair_command = uv_tool_install_hint("uv tool install --editable '.[slack]' --overrides uv-overrides.txt")
        logger.warning("slack_sdk not installed — reinstall with: %s", repair_command)
        return

    web_client = WebClient(token=bot_token)
    client = SocketModeClient(app_token=app_token, web_client=web_client)
    identity = _probe_own_identity(web_client, overlay_name)

    def _handle(_sm_client: BaseSocketModeClient, req: SocketModeRequest) -> None:
        client.send_socket_mode_response(SocketModeResponse(envelope_id=req.envelope_id))
        if req.type == "interactive":
            _hand_over_tap(overlay_name, req.payload or {}, callbacks.on_action)
            return
        if req.type != "events_api":
            return
        event = (req.payload or {}).get("event", {})
        deliver_event(
            overlay_name,
            event,
            context=DeliveryContext(queues=queues, identity=identity, on_event=callbacks.on_event, seen=seen),
        )

    def _on_message(_sm_client: BaseSocketModeClient, message: dict, _raw_message: str | None) -> None:
        if message.get("type") == "hello":
            liveness.greet(client.session_id())

    client.socket_mode_request_listeners.append(_handle)
    client.message_listeners.append(_on_message)
    client.connect()
    logger.info("[%s] Socket Mode connected", overlay_name)

    _serve_until_stopped(overlay=overlay_name, stop_event=stop_event, client=client, liveness=liveness, seen=seen)

    client.close()
    logger.info("[%s] Socket Mode disconnected", overlay_name)


def run_listener(
    overlays: list[tuple[str, str, str]],
    *,
    paths: QueuePaths | None = None,
    on_event: OnEvent | None = None,
    on_action: OnAction | None = None,
    on_connect: OnConnect | None = None,
) -> None:
    paths = paths or QueuePaths.default()
    # Read once, before any overlay thread can restamp the file, so each first hello owes the pre-restart stamp.
    beat = read_heartbeat(paths.heartbeat)
    callbacks = ListenerCallbacks(on_event=on_event, on_action=on_action)
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
                "queues": paths,
                "stop_event": stop,
                "callbacks": callbacks,
                "liveness": ConnectionLiveness(
                    seed=beat.stamp_for(overlay[0]) if beat is not None else None,
                    on_connect=on_connect,
                    heartbeat_path=paths.heartbeat,
                ),
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
