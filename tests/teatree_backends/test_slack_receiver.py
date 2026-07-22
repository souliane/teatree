"""Tests for the Slack Socket Mode event queue (no real WebSocket connections)."""

import json
import threading
from pathlib import Path
from unittest.mock import MagicMock, patch

from teatree.backends.slack.receiver import (
    QueuePaths,
    _enqueue,
    _run_single_overlay,
    commit_drain,
    default_queue_path,
    default_reactions_queue_path,
    drain_event_queue,
    drain_reactions_queue,
    run_listener,
)


def _queues(tmp_path: Path) -> QueuePaths:
    return QueuePaths(events=tmp_path / "events.jsonl", reactions=tmp_path / "reactions.jsonl")


class TestDefaultQueuePath:
    def test_uses_xdg_data_home(self, monkeypatch) -> None:
        monkeypatch.setenv("XDG_DATA_HOME", "/custom/data")
        assert default_queue_path() == Path("/custom/data/teatree/slack-events.jsonl")

    def test_falls_back_to_home(self, monkeypatch) -> None:
        monkeypatch.delenv("XDG_DATA_HOME", raising=False)
        result = default_queue_path()
        assert result.name == "slack-events.jsonl"
        assert "teatree" in str(result)

    def test_reactions_queue_path_uses_xdg_data_home(self, monkeypatch) -> None:
        monkeypatch.setenv("XDG_DATA_HOME", "/custom/data")
        assert default_reactions_queue_path() == Path("/custom/data/teatree/slack-reactions.jsonl")

    def test_reactions_queue_path_falls_back_to_home(self, monkeypatch) -> None:
        monkeypatch.delenv("XDG_DATA_HOME", raising=False)
        result = default_reactions_queue_path()
        assert result.name == "slack-reactions.jsonl"
        assert "teatree" in str(result)


class TestEnqueue:
    def test_writes_json_line(self, tmp_path: Path) -> None:
        queue = tmp_path / "events.jsonl"
        _enqueue(queue, "myoverlay", {"type": "message", "text": "hello"})

        lines = queue.read_text(encoding="utf-8").strip().splitlines()
        assert len(lines) == 1
        data = json.loads(lines[0])
        assert data["overlay"] == "myoverlay"
        assert data["event"]["text"] == "hello"

    def test_appends_multiple_events(self, tmp_path: Path) -> None:
        queue = tmp_path / "events.jsonl"
        _enqueue(queue, "a", {"type": "message", "text": "first"})
        _enqueue(queue, "b", {"type": "app_mention", "text": "second"})

        lines = queue.read_text(encoding="utf-8").strip().splitlines()
        assert len(lines) == 2

    def test_creates_parent_dirs(self, tmp_path: Path) -> None:
        queue = tmp_path / "deep" / "nested" / "events.jsonl"
        _enqueue(queue, "x", {"type": "message"})
        assert queue.is_file()


class TestDrainEventQueue:
    def test_returns_empty_when_no_file(self, tmp_path: Path) -> None:
        assert drain_event_queue(tmp_path / "missing.jsonl") == []

    def test_drains_and_removes_file(self, tmp_path: Path) -> None:
        queue = tmp_path / "events.jsonl"
        _enqueue(queue, "ov1", {"type": "message", "text": "hi"})
        _enqueue(queue, "ov2", {"type": "app_mention", "text": "hey"})

        events = drain_event_queue(queue)

        assert len(events) == 2
        assert events[0]["overlay"] == "ov1"
        assert events[1]["overlay"] == "ov2"
        assert not queue.is_file()

    def test_skips_malformed_lines(self, tmp_path: Path) -> None:
        queue = tmp_path / "events.jsonl"
        queue.write_text('{"overlay":"a","event":{}}\nNOT_JSON\n{"overlay":"b","event":{}}\n', encoding="utf-8")

        events = drain_event_queue(queue)

        assert len(events) == 2

    def test_handles_rename_failure_gracefully(self, tmp_path: Path) -> None:
        queue = tmp_path / "events.jsonl"
        queue.write_text("{}\n", encoding="utf-8")
        draining = queue.with_suffix(".draining")
        draining.mkdir()

        events = drain_event_queue(queue)
        assert events == []


class TestDrainCommitRecovery:
    """Recover-then-drain keeps the backing file alive until the caller commits.

    The file survives a crash between the in-memory drain and the caller's
    durable persist, so mentions are not lost (Slack never retries
    ``app_mention`` delivery).
    """

    def test_drain_leaves_backing_file_until_committed(self, tmp_path: Path) -> None:
        queue = tmp_path / "events.jsonl"
        _enqueue(queue, "ov1", {"type": "app_mention", "text": "hi"})

        events = drain_event_queue(queue)

        assert len(events) == 1
        # Live queue is renamed away, but the drained data still lives on disk
        # (in the .draining file) until the caller commits after persisting.
        assert not queue.is_file()
        assert queue.with_suffix(".draining").is_file()

    def test_crash_after_drain_before_persist_recovers_events(self, tmp_path: Path) -> None:
        queue = tmp_path / "events.jsonl"
        _enqueue(queue, "ov1", {"type": "app_mention", "text": "hi"})
        _enqueue(queue, "ov2", {"type": "app_mention", "text": "hey"})

        # First drain reads the events into memory. The process crashes here,
        # before the caller persists them durably — commit_drain is never reached.
        first = drain_event_queue(queue)
        assert len(first) == 2

        # Next tick: the events must be recoverable, not lost.
        recovered = drain_event_queue(queue)
        assert len(recovered) == 2
        assert {e["overlay"] for e in recovered} == {"ov1", "ov2"}

    def test_commit_drain_removes_backing_file(self, tmp_path: Path) -> None:
        queue = tmp_path / "events.jsonl"
        _enqueue(queue, "ov1", {"type": "app_mention", "text": "hi"})

        events = drain_event_queue(queue)
        assert len(events) == 1

        commit_drain(queue)

        # Once committed (after durable persist), the data is gone and a
        # subsequent drain returns nothing.
        assert not queue.with_suffix(".draining").is_file()
        assert drain_event_queue(queue) == []

    def test_new_event_during_recovery_is_not_lost(self, tmp_path: Path) -> None:
        queue = tmp_path / "events.jsonl"
        _enqueue(queue, "ov1", {"type": "app_mention", "text": "first"})

        # Crash after draining ov1 (no commit) — ov1 sits in the .draining file.
        drain_event_queue(queue)

        # A new event arrives on the live queue before the next drain.
        _enqueue(queue, "ov2", {"type": "app_mention", "text": "second"})

        # Recovery drains only the leftover and never touches the live file,
        # so the concurrent enqueue cannot be dropped.
        recovered = drain_event_queue(queue)
        assert {e["overlay"] for e in recovered} == {"ov1"}
        assert queue.is_file()

        # After committing the recovered batch, the live event drains next.
        commit_drain(queue)
        follow_up = drain_event_queue(queue)
        assert {e["overlay"] for e in follow_up} == {"ov2"}


class TestRunSingleOverlay:
    def test_returns_immediately_when_slack_sdk_missing(self, tmp_path: Path) -> None:
        stop = threading.Event()
        stop.set()
        with patch.dict("sys.modules", {"slack_sdk": None, "slack_sdk.socket_mode": None}):
            _run_single_overlay(
                overlay=("test", "xapp-test", "xoxb-test"),
                queues=_queues(tmp_path),
                stop_event=stop,
            )

    def test_connects_and_stops_on_event(self, tmp_path: Path) -> None:
        import slack_sdk.socket_mode  # noqa: PLC0415 — optional slack_sdk dep
        import slack_sdk.web  # noqa: PLC0415 — optional slack_sdk dep

        mock_client = MagicMock()
        mock_client.socket_mode_request_listeners = []
        stop = threading.Event()

        def fake_connect() -> None:
            stop.set()

        mock_client.connect = fake_connect

        with (
            patch.object(slack_sdk.socket_mode, "SocketModeClient", return_value=mock_client),
            patch.object(slack_sdk.web, "WebClient"),
        ):
            _run_single_overlay(
                overlay=("ov", "xapp", "xoxb"),
                queues=_queues(tmp_path),
                stop_event=stop,
            )

        mock_client.close.assert_called_once()
        assert len(mock_client.socket_mode_request_listeners) == 1

    def test_handler_enqueues_mention_events(self, tmp_path: Path) -> None:
        import slack_sdk.socket_mode  # noqa: PLC0415 — optional slack_sdk dep
        import slack_sdk.web  # noqa: PLC0415 — optional slack_sdk dep
        from slack_sdk.socket_mode.request import SocketModeRequest  # noqa: PLC0415 — optional dep

        mock_client = MagicMock()
        mock_client.socket_mode_request_listeners = []
        stop = threading.Event()
        queue = tmp_path / "events.jsonl"
        handler_ref: list = []

        def fake_connect() -> None:
            handler_ref.extend(mock_client.socket_mode_request_listeners)
            req = SocketModeRequest(
                type="events_api",
                envelope_id="e1",
                payload={"event": {"type": "app_mention", "text": "hello", "ts": "1.0"}},
            )
            handler_ref[0](mock_client, req)
            stop.set()

        mock_client.connect = fake_connect

        with (
            patch.object(slack_sdk.socket_mode, "SocketModeClient", return_value=mock_client),
            patch.object(slack_sdk.web, "WebClient"),
        ):
            _run_single_overlay(
                overlay=("ov", "xapp", "xoxb"),
                queues=_queues(tmp_path),
                stop_event=stop,
            )

        events = drain_event_queue(queue)
        assert len(events) == 1
        assert events[0]["event"]["type"] == "app_mention"

    def test_handler_signals_wake_on_enqueued_event(self, tmp_path: Path) -> None:
        import slack_sdk.socket_mode  # noqa: PLC0415 — optional slack_sdk dep
        import slack_sdk.web  # noqa: PLC0415 — optional slack_sdk dep
        from slack_sdk.socket_mode.request import SocketModeRequest  # noqa: PLC0415 — optional dep

        mock_client = MagicMock()
        mock_client.socket_mode_request_listeners = []
        stop = threading.Event()
        on_event = MagicMock()
        handler_ref: list = []

        def fake_connect() -> None:
            handler_ref.extend(mock_client.socket_mode_request_listeners)
            req = SocketModeRequest(
                type="events_api",
                envelope_id="e1",
                payload={"event": {"type": "app_mention", "text": "hello", "ts": "1.0"}},
            )
            handler_ref[0](mock_client, req)
            stop.set()

        mock_client.connect = fake_connect

        with (
            patch.object(slack_sdk.socket_mode, "SocketModeClient", return_value=mock_client),
            patch.object(slack_sdk.web, "WebClient"),
        ):
            _run_single_overlay(
                overlay=("ov", "xapp", "xoxb"),
                queues=_queues(tmp_path),
                stop_event=stop,
                on_event=on_event,
            )

        # The inbound event fires the drain signal immediately — no cadence wait.
        on_event.assert_called_once_with()

    def test_handler_does_not_signal_for_filtered_bot_message(self, tmp_path: Path) -> None:
        import slack_sdk.socket_mode  # noqa: PLC0415 — optional slack_sdk dep
        import slack_sdk.web  # noqa: PLC0415 — optional slack_sdk dep
        from slack_sdk.socket_mode.request import SocketModeRequest  # noqa: PLC0415 — optional dep

        mock_client = MagicMock()
        mock_client.socket_mode_request_listeners = []
        stop = threading.Event()
        on_event = MagicMock()
        handler_ref: list = []

        def fake_connect() -> None:
            handler_ref.extend(mock_client.socket_mode_request_listeners)
            req = SocketModeRequest(
                type="events_api",
                envelope_id="b1",
                payload={"event": {"type": "message", "subtype": "bot_message", "ts": "1.0"}},
            )
            handler_ref[0](mock_client, req)
            stop.set()

        mock_client.connect = fake_connect

        with (
            patch.object(slack_sdk.socket_mode, "SocketModeClient", return_value=mock_client),
            patch.object(slack_sdk.web, "WebClient"),
        ):
            _run_single_overlay(
                overlay=("ov", "xapp", "xoxb"),
                queues=_queues(tmp_path),
                stop_event=stop,
                on_event=on_event,
            )

        # A filtered event is never enqueued, so it never signals the drain.
        on_event.assert_not_called()

    def test_handler_signal_failure_does_not_break_receiver(self, tmp_path: Path) -> None:
        import slack_sdk.socket_mode  # noqa: PLC0415 — optional slack_sdk dep
        import slack_sdk.web  # noqa: PLC0415 — optional slack_sdk dep
        from slack_sdk.socket_mode.request import SocketModeRequest  # noqa: PLC0415 — optional dep

        mock_client = MagicMock()
        mock_client.socket_mode_request_listeners = []
        stop = threading.Event()
        queue = tmp_path / "events.jsonl"

        def boom() -> None:
            message = "db unavailable"
            raise RuntimeError(message)

        handler_ref: list = []

        def fake_connect() -> None:
            handler_ref.extend(mock_client.socket_mode_request_listeners)
            req = SocketModeRequest(
                type="events_api",
                envelope_id="e1",
                payload={"event": {"type": "app_mention", "text": "hi", "ts": "1.0"}},
            )
            handler_ref[0](mock_client, req)
            stop.set()

        mock_client.connect = fake_connect

        with (
            patch.object(slack_sdk.socket_mode, "SocketModeClient", return_value=mock_client),
            patch.object(slack_sdk.web, "WebClient"),
        ):
            _run_single_overlay(
                overlay=("ov", "xapp", "xoxb"),
                queues=_queues(tmp_path),
                stop_event=stop,
                on_event=boom,
            )

        # The durable JSONL write still landed even though the wake signal raised.
        events = drain_event_queue(queue)
        assert len(events) == 1

    def test_handler_filters_bot_messages(self, tmp_path: Path) -> None:
        import slack_sdk.socket_mode  # noqa: PLC0415 — optional slack_sdk dep
        import slack_sdk.web  # noqa: PLC0415 — optional slack_sdk dep
        from slack_sdk.socket_mode.request import SocketModeRequest  # noqa: PLC0415 — optional dep

        mock_client = MagicMock()
        mock_client.socket_mode_request_listeners = []
        stop = threading.Event()
        queue = tmp_path / "events.jsonl"
        handler_ref: list = []

        def fake_connect() -> None:
            handler_ref.extend(mock_client.socket_mode_request_listeners)
            for subtype in ("bot_message", "message_changed", "message_deleted"):
                req = SocketModeRequest(
                    type="events_api",
                    envelope_id=f"e-{subtype}",
                    payload={"event": {"type": "message", "subtype": subtype, "ts": "1.0"}},
                )
                handler_ref[0](mock_client, req)
            stop.set()

        mock_client.connect = fake_connect

        with (
            patch.object(slack_sdk.socket_mode, "SocketModeClient", return_value=mock_client),
            patch.object(slack_sdk.web, "WebClient"),
        ):
            _run_single_overlay(
                overlay=("ov", "xapp", "xoxb"),
                queues=_queues(tmp_path),
                stop_event=stop,
            )

        events = drain_event_queue(queue)
        assert events == []

    def test_handler_routes_reaction_added_to_reactions_queue(self, tmp_path: Path) -> None:
        import slack_sdk.socket_mode  # noqa: PLC0415 — optional slack_sdk dep
        import slack_sdk.web  # noqa: PLC0415 — optional slack_sdk dep
        from slack_sdk.socket_mode.request import SocketModeRequest  # noqa: PLC0415 — optional dep

        mock_client = MagicMock()
        mock_client.socket_mode_request_listeners = []
        stop = threading.Event()
        events_queue = tmp_path / "events.jsonl"
        reactions_queue = tmp_path / "reactions.jsonl"
        handler_ref: list = []

        def fake_connect() -> None:
            handler_ref.extend(mock_client.socket_mode_request_listeners)
            req = SocketModeRequest(
                type="events_api",
                envelope_id="r1",
                payload={
                    "event": {
                        "type": "reaction_added",
                        "user": "U0",
                        "reaction": "thumbsup",
                        "item": {"type": "message", "channel": "C1", "ts": "1.0"},
                        "event_ts": "1.0",
                    }
                },
            )
            handler_ref[0](mock_client, req)
            stop.set()

        mock_client.connect = fake_connect

        with (
            patch.object(slack_sdk.socket_mode, "SocketModeClient", return_value=mock_client),
            patch.object(slack_sdk.web, "WebClient"),
        ):
            _run_single_overlay(
                overlay=("ov", "xapp", "xoxb"),
                queues=QueuePaths(events=events_queue, reactions=reactions_queue),
                stop_event=stop,
            )

        # Reaction landed in the reactions queue, not the events queue.
        assert not events_queue.is_file()
        reaction_events = drain_reactions_queue(reactions_queue)
        assert len(reaction_events) == 1
        assert reaction_events[0]["event"]["type"] == "reaction_added"

    def test_handler_ignores_non_events_api(self, tmp_path: Path) -> None:
        import slack_sdk.socket_mode  # noqa: PLC0415 — optional slack_sdk dep
        import slack_sdk.web  # noqa: PLC0415 — optional slack_sdk dep
        from slack_sdk.socket_mode.request import SocketModeRequest  # noqa: PLC0415 — optional dep

        mock_client = MagicMock()
        mock_client.socket_mode_request_listeners = []
        stop = threading.Event()
        queue = tmp_path / "events.jsonl"

        def fake_connect() -> None:
            handler = mock_client.socket_mode_request_listeners[0]
            req = SocketModeRequest(type="slash_commands", envelope_id="e1", payload={})
            handler(mock_client, req)
            stop.set()

        mock_client.connect = fake_connect

        with (
            patch.object(slack_sdk.socket_mode, "SocketModeClient", return_value=mock_client),
            patch.object(slack_sdk.web, "WebClient"),
        ):
            _run_single_overlay(
                overlay=("ov", "xapp", "xoxb"),
                queues=_queues(tmp_path),
                stop_event=stop,
            )

        assert not queue.is_file()


class TestRunListener:
    def test_returns_when_no_overlay_threads(self, tmp_path: Path) -> None:
        run_listener([], queue_path=tmp_path / "events.jsonl")

    def test_starts_threads_and_stops_on_signal(self, tmp_path: Path) -> None:
        import signal as sig  # noqa: PLC0415

        started = threading.Event()

        def fake_overlay(**kwargs: object) -> None:
            started.set()
            stop_event = kwargs["stop_event"]
            stop_event.wait(timeout=5.0)

        def run_and_signal() -> None:
            started.wait(timeout=2.0)
            sig.raise_signal(sig.SIGINT)

        with patch("teatree.backends.slack.receiver._run_single_overlay", side_effect=fake_overlay):
            trigger = threading.Thread(target=run_and_signal, daemon=True)
            trigger.start()
            run_listener([("ov", "xapp", "xoxb")], queue_path=tmp_path / "events.jsonl")
            trigger.join(timeout=2.0)
