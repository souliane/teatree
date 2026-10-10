"""A tap on an owner-question button reaches the click handler, after the envelope is acknowledged (#4990)."""

import threading
from collections.abc import Callable
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from teatree.backends.slack.receiver import QueuePaths, _run_single_overlay

_CLICK = {
    "type": "block_actions",
    "user": {"id": "U0DEMOOWNER"},
    "channel": {"id": "D0DEMOOWNER"},
    "message": {"ts": "1700000000.000100"},
    "actions": [{"action_id": "owner-question-option-1", "value": "1", "text": {"type": "plain_text", "text": "Done"}}],
}


def _drive(
    tmp_path: Path, *, request_type: str, payload: dict, on_action: Callable[[str, dict], None] | None
) -> MagicMock:
    """Run one envelope through the real receiver callback; return the client that must acknowledge it."""
    import slack_sdk.socket_mode  # noqa: PLC0415 — optional slack_sdk dep
    import slack_sdk.web  # noqa: PLC0415 — optional slack_sdk dep
    from slack_sdk.socket_mode.request import SocketModeRequest  # noqa: PLC0415 — optional dep

    client = MagicMock()
    client.socket_mode_request_listeners = []
    stop = threading.Event()

    def fake_connect() -> None:
        client.socket_mode_request_listeners[0](
            client, SocketModeRequest(type=request_type, envelope_id="e-click", payload=payload)
        )
        stop.set()

    client.connect = fake_connect
    web_client = MagicMock()
    web_client.auth_test.return_value.data = {"ok": True, "user_id": "U0BOT", "bot_id": "B0BOT"}
    with (
        patch.object(slack_sdk.socket_mode, "SocketModeClient", return_value=client),
        patch.object(slack_sdk.web, "WebClient", return_value=web_client),
    ):
        _run_single_overlay(
            overlay=("ov", "xapp", "xoxb"),
            queues=QueuePaths(events=tmp_path / "events.jsonl", reactions=tmp_path / "reactions.jsonl"),
            stop_event=stop,
            on_action=on_action,
        )
    return client


class TestTheInteractiveEnvelope:
    def test_it_is_acknowledged_and_its_block_actions_payload_reaches_on_action_exactly_once(
        self, tmp_path: Path
    ) -> None:
        on_action = MagicMock()

        client = _drive(tmp_path, request_type="interactive", payload=_CLICK, on_action=on_action)

        client.send_socket_mode_response.assert_called_once()
        assert client.send_socket_mode_response.call_args.args[0].envelope_id == "e-click"
        on_action.assert_called_once_with("ov", _CLICK)

    def test_the_envelope_is_acknowledged_before_the_handler_runs(self, tmp_path: Path) -> None:
        order: list[str] = []
        on_action = MagicMock(side_effect=lambda *_: order.append("handled"))

        def run() -> MagicMock:
            return _drive(tmp_path, request_type="interactive", payload=_CLICK, on_action=on_action)

        with patch("slack_sdk.socket_mode.response.SocketModeResponse", side_effect=lambda **_: order.append("acked")):
            run()

        assert order[:1] == ["acked"]
        assert order[-1] == "handled"

    @pytest.mark.parametrize("payload_type", ["view_submission", "shortcut", ""])
    def test_a_payload_that_is_no_tap_is_acknowledged_and_ignored(self, tmp_path: Path, payload_type: str) -> None:
        on_action = MagicMock()

        client = _drive(
            tmp_path, request_type="interactive", payload={**_CLICK, "type": payload_type}, on_action=on_action
        )

        client.send_socket_mode_response.assert_called_once()
        on_action.assert_not_called()

    def test_a_handler_that_raises_never_breaks_the_listener(self, tmp_path: Path) -> None:
        on_action = MagicMock(side_effect=RuntimeError("database is locked"))

        client = _drive(tmp_path, request_type="interactive", payload=_CLICK, on_action=on_action)

        on_action.assert_called_once()
        client.send_socket_mode_response.assert_called_once()

    def test_an_events_api_envelope_never_reaches_on_action(self, tmp_path: Path) -> None:
        on_action = MagicMock()

        _drive(
            tmp_path,
            request_type="events_api",
            payload={"event": {"type": "message", "user": "U0DEMOOWNER", "ts": "1.0"}},
            on_action=on_action,
        )

        on_action.assert_not_called()

    def test_without_a_handler_a_tap_is_still_acknowledged(self, tmp_path: Path) -> None:
        client = _drive(tmp_path, request_type="interactive", payload=_CLICK, on_action=None)

        client.send_socket_mode_response.assert_called_once()
