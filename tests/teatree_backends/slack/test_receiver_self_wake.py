"""The answer-cycle wake must not fire on the app's own posts (#4707).

The bot's ``chat.postMessage`` output returns through Socket Mode as a plain
``message`` event carrying the bot's own ``user`` / ``bot_id`` — Slack stamps
``subtype=bot_message`` only on some post shapes, so the receiver's subtype drop
never caught it. Waking the answer cycle on that event let the cycle feed on its
own replies: measured at 2,391 ``chat.postMessage`` in 24h, a wake every ~2s, and
466 ``conversations.replies`` polls of ONE thread in two hours.

``filter_self_messages`` already refuses to ANSWER such a row, but it runs in the
scanner — downstream of the wake — so every self-post still bought a full cycle
and two Slack reads whatever the answer decision was. These tests pin the guard
at the trigger, where the gain is introduced, and drive it through the receiver's
real Socket Mode callback rather than the predicate alone.
"""

import threading
from pathlib import Path
from unittest.mock import MagicMock, patch

from teatree.backends.slack.receiver import QueuePaths, _probe_own_identity, _run_single_overlay
from teatree.backends.slack.self_identity import OwnSlackIdentity, is_self_originated

_OWN = OwnSlackIdentity(user_id="U0BOT", bot_id="B0BOT")
_AUTH_OK = {"ok": True, "user_id": _OWN.user_id, "bot_id": _OWN.bot_id}


def _queues(tmp_path: Path) -> QueuePaths:
    return QueuePaths(events=tmp_path / "events.jsonl", reactions=tmp_path / "reactions.jsonl")


def _drive(tmp_path: Path, event: dict, *, auth_body: dict | None) -> MagicMock:
    """Run one inbound *event* through the real receiver callback; return the wake mock."""
    import slack_sdk.socket_mode  # noqa: PLC0415 — optional slack_sdk dep
    import slack_sdk.web  # noqa: PLC0415 — optional slack_sdk dep
    from slack_sdk.socket_mode.request import SocketModeRequest  # noqa: PLC0415 — optional dep

    mock_client = MagicMock()
    mock_client.socket_mode_request_listeners = []
    web_client = MagicMock()
    web_client.auth_test.return_value.data = auth_body
    stop = threading.Event()
    on_event = MagicMock()

    def fake_connect() -> None:
        handler = mock_client.socket_mode_request_listeners[0]
        handler(mock_client, SocketModeRequest(type="events_api", envelope_id="e1", payload={"event": event}))
        stop.set()

    mock_client.connect = fake_connect
    with (
        patch.object(slack_sdk.socket_mode, "SocketModeClient", return_value=mock_client),
        patch.object(slack_sdk.web, "WebClient", return_value=web_client),
    ):
        _run_single_overlay(
            overlay=("ov", "xapp", "xoxb"),
            queues=_queues(tmp_path),
            stop_event=stop,
            on_event=on_event,
        )
    return on_event


class TestReceiverCallbackDoesNotWakeOnItsOwnPosts:
    def test_a_post_carrying_the_bot_user_id_raises_no_wake(self, tmp_path: Path) -> None:
        on_event = _drive(tmp_path, {"type": "message", "user": _OWN.user_id, "ts": "1.0"}, auth_body=_AUTH_OK)

        on_event.assert_not_called()

    def test_a_post_carrying_the_bot_id_raises_no_wake(self, tmp_path: Path) -> None:
        on_event = _drive(tmp_path, {"type": "message", "bot_id": _OWN.bot_id, "ts": "1.0"}, auth_body=_AUTH_OK)

        on_event.assert_not_called()

    def test_a_genuine_user_message_still_enqueues_exactly_one_wake(self, tmp_path: Path) -> None:
        # The control. Without it the guard above is satisfiable by a receiver that
        # refuses every event, which disables the whole event-driven answer path
        # rather than fixing the loop.
        on_event = _drive(tmp_path, {"type": "message", "user": "U0HUMAN", "ts": "1.0"}, auth_body=_AUTH_OK)

        on_event.assert_called_once_with()


class TestAnUnresolvedIdentityStillBreaksTheLoop:
    """The probe can fail; the guard may not go with it.

    Failing closed on an unresolved identity would kill the wake for the owner's
    OWN messages too — a silent regression to cadence latency that survives until
    someone restarts the listener. The event's own fields carry enough.
    """

    def test_a_bot_post_raises_no_wake_without_an_identity(self, tmp_path: Path) -> None:
        on_event = _drive(tmp_path, {"type": "message", "bot_id": "B0BOT", "ts": "1.0"}, auth_body=None)

        on_event.assert_not_called()

    def test_a_user_message_still_wakes_without_an_identity(self, tmp_path: Path) -> None:
        on_event = _drive(tmp_path, {"type": "message", "user": "U0HUMAN", "ts": "1.0"}, auth_body=None)

        on_event.assert_called_once_with()


class TestProbeOwnIdentity:
    def test_reads_the_ids_off_the_auth_test_body(self) -> None:
        client = MagicMock()
        client.auth_test.return_value.data = _AUTH_OK

        assert _probe_own_identity(client, "ov") == _OWN

    def test_a_raising_probe_degrades_to_no_identity(self) -> None:
        client = MagicMock()
        client.auth_test.side_effect = RuntimeError("ratelimited")

        assert _probe_own_identity(client, "ov") is None

    def test_a_non_dict_body_degrades_to_no_identity(self) -> None:
        client = MagicMock()
        client.auth_test.return_value.data = b"binary"

        assert _probe_own_identity(client, "ov") is None

    def test_probed_once_per_connection_not_once_per_event(self, tmp_path: Path) -> None:
        # The contributing defect: ~7 auth.test a minute, and being fail-closed a
        # rate-limit there disabled the self-check exactly when traffic peaked.
        import slack_sdk.socket_mode  # noqa: PLC0415 — optional slack_sdk dep
        import slack_sdk.web  # noqa: PLC0415 — optional slack_sdk dep
        from slack_sdk.socket_mode.request import SocketModeRequest  # noqa: PLC0415 — optional dep

        mock_client = MagicMock()
        mock_client.socket_mode_request_listeners = []
        web_client = MagicMock()
        web_client.auth_test.return_value.data = _AUTH_OK
        stop = threading.Event()

        def fake_connect() -> None:
            handler = mock_client.socket_mode_request_listeners[0]
            for i in range(5):
                payload = {"event": {"type": "message", "user": "U0HUMAN", "ts": f"{i}.0"}}
                handler(mock_client, SocketModeRequest(type="events_api", envelope_id=f"e{i}", payload=payload))
            stop.set()

        mock_client.connect = fake_connect
        with (
            patch.object(slack_sdk.socket_mode, "SocketModeClient", return_value=mock_client),
            patch.object(slack_sdk.web, "WebClient", return_value=web_client),
        ):
            _run_single_overlay(
                overlay=("ov", "xapp", "xoxb"),
                queues=_queues(tmp_path),
                stop_event=stop,
                on_event=MagicMock(),
            )

        assert web_client.auth_test.call_count == 1


class TestIsSelfOriginated:
    def test_an_on_behalf_post_is_self_originated_with_no_identity_at_all(self) -> None:
        # api_app_id is what drop_on_behalf_messages already keys on downstream,
        # and it is stamped by Slack rather than derived, so it needs no probe.
        assert is_self_originated({"user": "U0HUMAN", "api_app_id": "A123"}, None) is True

    def test_a_third_party_bot_is_self_originated_because_bot_id_cannot_be_attributed(self) -> None:
        # Deliberate over-reach: without the identity, bot_id is the only thing
        # that separates our own returning post from a human's, so a foreign bot's
        # DM waits for the cadence chain instead of waking one immediately.
        assert is_self_originated({"bot_id": "B_OTHER"}, _OWN) is True

    def test_a_blank_identity_cannot_vouch_for_a_human_message(self) -> None:
        assert is_self_originated({"user": "U0HUMAN"}, OwnSlackIdentity(user_id="", bot_id="")) is False

    def test_a_non_string_bot_id_is_not_treated_as_authorship(self) -> None:
        assert is_self_originated({"user": "U0HUMAN", "bot_id": 0}, None) is False
