"""The receiver's hello hook: the catch-up a connection owes, and the stamping it gates.

Driven through the real ``_run_single_overlay`` / ``run_listener`` with a fake Socket
Mode client that fires ``hello`` through ``message_listeners`` exactly as slack_sdk
does, and a shortened serve tick, stamp interval and retry backoff.
"""

import json
import signal
import threading
import time
from collections.abc import Callable, Generator, Iterator
from contextlib import contextmanager
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
import slack_sdk.socket_mode
import slack_sdk.web

from teatree.backends.slack.listener_heartbeat import ConnectionLiveness, read_heartbeat
from teatree.backends.slack.receiver import DeliveredEvents, QueuePaths, _run_single_overlay, run_listener

_TICK = 0.01


class FakeSocket:
    def __init__(self, *, on_connect: Callable[["FakeSocket"], None] | None = None) -> None:
        self.socket_mode_request_listeners: list = []
        self.message_listeners: list = []
        self.session = "s1"
        self._on_connect = on_connect

    def session_id(self) -> str:
        return self.session

    def is_connected(self) -> bool:
        return True

    def connect(self) -> None:
        if self._on_connect is not None:
            self._on_connect(self)

    def close(self) -> None:
        pass

    def send_socket_mode_response(self, _response: object) -> None:
        pass

    def hello(self) -> None:
        for listener in self.message_listeners:
            listener(self, {"type": "hello"}, '{"type":"hello"}')


@pytest.fixture(autouse=True)
def _fast_loop() -> Iterator[None]:
    with (
        patch("teatree.backends.slack.receiver._SERVE_TICK_SECONDS", _TICK),
        patch("teatree.backends.slack.receiver.HEARTBEAT_INTERVAL_SECONDS", 0.02),
        patch("teatree.backends.slack.listener_heartbeat.RETRY_BACKOFF_SECONDS", (0.05,)),
    ):
        yield


def _wait_for(predicate: Callable[[], bool], timeout: float = 5.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(_TICK)
    return False


def _stamp(path: Path, overlay: str = "ov") -> float | None:
    beat = read_heartbeat(path)
    return beat.stamp_for(overlay) if beat is not None else None


@contextmanager
def _running(tmp_path: Path, socket: FakeSocket, liveness: ConnectionLiveness) -> Generator[None]:
    stop = threading.Event()
    with (
        patch.object(slack_sdk.socket_mode, "SocketModeClient", return_value=socket),
        patch.object(slack_sdk.web, "WebClient", return_value=MagicMock()),
    ):
        thread = threading.Thread(
            target=_run_single_overlay,
            kwargs={
                "overlay": ("ov", "xapp", "xoxb"),
                "queues": QueuePaths(events=tmp_path / "events.jsonl", reactions=tmp_path / "reactions.jsonl"),
                "stop_event": stop,
                "liveness": liveness,
            },
            daemon=True,
        )
        thread.start()
        try:
            assert _wait_for(lambda: bool(socket.message_listeners))
            yield
        finally:
            stop.set()
            thread.join(timeout=5.0)


def test_no_stamp_before_the_current_session_greets(tmp_path: Path) -> None:
    beat = tmp_path / "beat.json"
    socket = FakeSocket()

    with _running(tmp_path, socket, ConnectionLiveness(on_connect=MagicMock(), heartbeat_path=beat)):
        time.sleep(20 * _TICK)
        assert not beat.exists()
        socket.hello()
        assert _wait_for(lambda: _stamp(beat) is not None)


def test_no_prior_heartbeat_allows_the_first_stamp_without_catch_up(tmp_path: Path) -> None:
    beat = tmp_path / "beat.json"
    socket = FakeSocket()
    on_connect = MagicMock()

    with _running(tmp_path, socket, ConnectionLiveness(seed=None, on_connect=on_connect, heartbeat_path=beat)):
        socket.hello()
        assert _wait_for(lambda: _stamp(beat) is not None)

    on_connect.assert_not_called()


def test_a_hello_message_reaches_greet(tmp_path: Path) -> None:
    beat = tmp_path / "beat.json"
    socket = FakeSocket()
    on_connect = MagicMock()

    with _running(tmp_path, socket, ConnectionLiveness(seed=1000.0, on_connect=on_connect, heartbeat_path=beat)):
        socket.hello()
        assert _wait_for(lambda: on_connect.call_count == 1)

    assert on_connect.call_args.args[:2] == ("ov", 1000.0)
    assert isinstance(on_connect.call_args.args[2], DeliveredEvents)


def test_hello_receives_the_live_delivery_memo(tmp_path: Path) -> None:
    beat = tmp_path / "beat.json"
    socket = FakeSocket()
    on_connect = MagicMock()
    event = {"type": "message", "channel": "D1", "channel_type": "im", "ts": "1001.0", "user": "U1"}

    with _running(tmp_path, socket, ConnectionLiveness(seed=1000.0, on_connect=on_connect, heartbeat_path=beat)):
        request = MagicMock(type="events_api", payload={"event": event}, envelope_id="e1")
        socket.socket_mode_request_listeners[0](socket, request)
        socket.hello()
        assert _wait_for(lambda: on_connect.call_count == 1)

    assert on_connect.call_args.args[2].as_mapping() == {"message:D1:1001.0": 1001.0}


def test_a_routine_refresh_stops_stamping_until_the_new_hello(tmp_path: Path) -> None:
    beat = tmp_path / "beat.json"
    socket = FakeSocket()
    on_connect = MagicMock()

    with _running(tmp_path, socket, ConnectionLiveness(on_connect=on_connect, heartbeat_path=beat)):
        socket.hello()
        assert _wait_for(lambda: _stamp(beat) is not None)
        socket.session = "s2"
        time.sleep(5 * _TICK)
        last = _stamp(beat)
        time.sleep(20 * _TICK)
        assert _stamp(beat) == last
        socket.hello()
        assert _wait_for(lambda: on_connect.call_count == 1)
        assert _wait_for(lambda: (_stamp(beat) or 0) > (last or 0))

    assert on_connect.call_args.args[:2] == ("ov", last)


def test_a_failing_catch_up_is_retried_and_withholds_the_stamp(tmp_path: Path) -> None:
    beat = tmp_path / "beat.json"
    socket = FakeSocket()
    outcomes = iter([RuntimeError("slack down"), None])
    calls: list[float] = []

    def on_connect(_overlay: str, alive_at: float, _seen: DeliveredEvents) -> None:
        calls.append(alive_at)
        if (outcome := next(outcomes)) is not None:
            raise outcome

    with _running(tmp_path, socket, ConnectionLiveness(seed=1000.0, on_connect=on_connect, heartbeat_path=beat)):
        socket.hello()
        assert _wait_for(lambda: len(calls) == 1)
        assert not beat.exists()
        assert _wait_for(lambda: len(calls) == 2)
        assert _wait_for(lambda: _stamp(beat) is not None)

    assert calls == [1000.0, 1000.0]


def test_first_hello_owes_the_pre_restart_stamp_after_a_sibling_overlay_stamped(tmp_path: Path) -> None:
    beat = tmp_path / "beat.json"
    beat.write_text(json.dumps({"updated_at": 1000, "interval_seconds": 15}), encoding="utf-8")
    calls: list[tuple[str, float]] = []

    def sibling_connects(socket: FakeSocket) -> None:
        socket.hello()

    def own_connects(socket: FakeSocket) -> None:
        assert _wait_for(lambda: _stamp(beat, "sib") not in {None, 1000.0})
        socket.hello()

    sockets = {"xapp-sib": FakeSocket(on_connect=sibling_connects), "xapp-ov": FakeSocket(on_connect=own_connects)}

    def stop_once_both_caught_up() -> None:
        _wait_for(lambda: {name for name, _ in calls} == {"sib", "ov"})
        signal.raise_signal(signal.SIGINT)

    trigger = threading.Thread(target=stop_once_both_caught_up, daemon=True)
    with (
        patch.object(slack_sdk.socket_mode, "SocketModeClient", side_effect=lambda **kw: sockets[kw["app_token"]]),
        patch.object(slack_sdk.web, "WebClient", return_value=MagicMock()),
    ):
        trigger.start()
        run_listener(
            [("sib", "xapp-sib", "xoxb"), ("ov", "xapp-ov", "xoxb")],
            paths=QueuePaths(events=tmp_path / "events.jsonl", reactions=tmp_path / "reactions.jsonl", heartbeat=beat),
            on_connect=lambda overlay, alive_at, _seen: calls.append((overlay, alive_at)),
        )
        trigger.join(timeout=5.0)

    assert ("ov", 1000.0) in calls
