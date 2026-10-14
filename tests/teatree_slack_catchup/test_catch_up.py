"""The public catch-up entry point recovers member-channel events through the user token."""

import time
from pathlib import Path
from unittest.mock import patch

import pytest

from teatree.backends.slack import http as slack_http
from teatree.backends.slack.bot import SlackBotBackend
from teatree.backends.slack.receiver import DeliveredEvents, QueuePaths
from teatree.loops.slack_catchup_job import CatchUpState
from teatree.loops.slack_inbound import catch_up_on_connect
from teatree.slack_catchup import catch_up
from teatree.types import RawAPIDict
from tests.integration.slack_bridge_e2e.conftest import FakeSlackTransport, serve_history


def test_mentions_and_owner_reactions_recover_only_from_member_channels(monkeypatch: pytest.MonkeyPatch) -> None:
    transport = FakeSlackTransport()
    monkeypatch.setattr(slack_http.httpx, "post", transport.post)
    monkeypatch.setattr(slack_http.httpx, "get", transport.get)
    stamp = f"{time.time() - 30:.6f}"
    transport.default_responses["users.conversations"] = {
        "ok": True,
        "channels": [{"id": "C_MEMBER"}],
    }
    transport.default_responses["search.messages"] = {
        "ok": True,
        "messages": {
            "matches": [
                {"ts": stamp, "user": "U_OTHER", "text": "<@U_OWNER> member", "channel": {"id": "C_MEMBER"}},
                {"ts": stamp, "user": "U_OTHER", "text": "<@U_OWNER> outsider", "channel": {"id": "C_OUTSIDER"}},
            ],
            "paging": {"pages": 1},
        },
    }
    transport.default_responses["reactions.list"] = {
        "ok": True,
        "items": [
            {
                "type": "message",
                "channel": "C_MEMBER",
                "message": {"ts": stamp, "reactions": [{"name": "eyes", "users": ["U_OWNER"]}]},
            },
            {
                "type": "message",
                "channel": "C_OUTSIDER",
                "message": {"ts": stamp, "reactions": [{"name": "eyes", "users": ["U_OWNER"]}]},
            },
        ],
    }
    backend = SlackBotBackend(bot_token="xoxb-bot", user_token="xoxp-user", user_id="U_OWNER")
    delivered: list[RawAPIDict] = []

    def deliver(event: RawAPIDict) -> bool:
        delivered.append(event)
        return True

    list(catch_up(backend, alive_at=time.time() - 180, deliver=deliver))

    assert [(event["type"], event.get("channel")) for event in delivered] == [
        ("app_mention", "C_MEMBER"),
        ("reaction_added", None),
    ]
    assert delivered[1]["item"] == {"type": "message", "channel": "C_MEMBER", "ts": stamp}
    assert {call.token for call in transport.calls_to("users.conversations")} == {"xoxp-user"}
    assert {call.token for call in transport.calls_to("search.messages")} == {"xoxp-user"}
    assert {call.token for call in transport.calls_to("reactions.list")} == {"xoxp-user"}


def test_missing_user_token_warns_and_keeps_bot_dm_recovery(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    transport = FakeSlackTransport()
    monkeypatch.setattr(slack_http.httpx, "post", transport.post)
    monkeypatch.setattr(slack_http.httpx, "get", transport.get)
    backend = SlackBotBackend(bot_token="xoxb-bot", user_id="U_OWNER")

    with caplog.at_level("WARNING"):
        list(catch_up(backend, alive_at=time.time() - 180, deliver=lambda _event: True, overlay="demo"))

    assert "demo" in caplog.text
    assert "channel mentions" in caplog.text
    assert "reactions" in caplog.text
    assert caplog.text.count("user token") == 1
    assert transport.calls_to("conversations.history")
    assert not transport.calls_to("search.messages")


def test_owner_dm_only_skips_user_token_reads_without_warning(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    transport = FakeSlackTransport()
    monkeypatch.setattr(slack_http.httpx, "post", transport.post)
    monkeypatch.setattr(slack_http.httpx, "get", transport.get)
    backend = SlackBotBackend(bot_token="xoxb-bot", user_token="xoxp-user", user_id="U_OWNER", owner_dm_only=True)

    with caplog.at_level("WARNING"):
        list(catch_up(backend, alive_at=time.time() - 180, deliver=lambda _event: True))

    assert transport.calls_to("conversations.history")
    assert not transport.calls_to("users.conversations")
    assert not transport.calls_to("search.messages")
    assert not transport.calls_to("reactions.list")
    assert not caplog.records


def test_fresh_hello_memos_do_not_repeat_dm_or_reaction_lines(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    transport = FakeSlackTransport()
    monkeypatch.setattr(slack_http.httpx, "post", transport.post)
    monkeypatch.setattr(slack_http.httpx, "get", transport.get)
    stamp = f"{time.time() - 30:.6f}"
    serve_history(
        transport,
        [{"ts": stamp, "user": "U_OWNER", "text": "ping", "reactions": [{"name": "eyes", "users": ["U_OWNER"]}]}],
    )
    backend = SlackBotBackend(bot_token="xoxb-bot", user_id="U_OWNER")
    queues = QueuePaths(events=tmp_path / "events.jsonl", reactions=tmp_path / "reactions.jsonl")
    with (
        patch("teatree.loops.slack_catchup_job.messaging_from_overlay", return_value=backend),
        patch.object(CatchUpState, "path", return_value=tmp_path / "state.json"),
        patch.object(QueuePaths, "default", return_value=queues),
        patch("teatree.loops.slack_inbound.record_slack_dm", return_value=True),
        patch("teatree.loops.slack_inbound.wake_slack_answer"),
    ):
        catch_up_on_connect("demo", time.time() - 180, DeliveredEvents())
        catch_up_on_connect("demo", time.time() - 180, DeliveredEvents())
        assert not CatchUpState.path("demo").exists()

    assert len(queues.events.read_text(encoding="utf-8").splitlines()) == 1
    assert len(queues.reactions.read_text(encoding="utf-8").splitlines()) == 1
