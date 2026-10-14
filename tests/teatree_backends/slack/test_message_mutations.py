import pytest

from teatree.backends.slack.bot_errors import SlackReadRefusedError
from teatree.backends.slack.message_mutations import delete_routed, read_for_write, read_message, update_routed
from teatree.slack_mrkdwn import wrap_slack_message
from teatree.types import RawAPIDict


def test_message_read_preserves_legacy_empty_and_audio_filter() -> None:
    assert read_message(lambda *_: {"ok": False}, lambda rows: rows, channel="C1", ts="1.0") == {}
    message = read_message(
        lambda *_: {"ok": True, "messages": [{"ts": "1.0", "text": "hello"}]},
        lambda rows: [{**rows[0], "text": "filtered"}],
        channel="C1",
        ts="1.0",
    )
    assert message["text"] == "filtered"


def test_mutation_read_uses_routed_token_and_refuses_failure() -> None:
    calls: list[str] = []

    def get(method: str, params: dict[str, str | int], *, token: str) -> RawAPIDict:
        calls.append(token)
        return {"ok": False, "error": "not_in_channel"}

    with pytest.raises(SlackReadRefusedError, match="not_in_channel"):
        read_for_write(get, token="xoxp-user", channel="C1", ts="1.0")
    assert calls == ["xoxp-user"]


def test_mutation_read_finds_reply_under_thread_root() -> None:
    def get(method: str, params: dict[str, str | int], *, token: str) -> RawAPIDict:
        assert method == "conversations.replies"
        assert params["ts"] == "root"
        assert token == "xoxp-user"
        return {"ok": True, "messages": [{"ts": "root"}, {"ts": "reply", "text": "before"}]}

    message = read_for_write(get, token="xoxp-user", channel="C1", ts="reply", thread_ts="root")
    assert message["text"] == "before"


def test_mutation_read_refuses_truncated_thread() -> None:
    def get(method: str, params: dict[str, str | int], *, token: str) -> RawAPIDict:
        _ = method, params, token
        return {"ok": True, "messages": [{"ts": "root"}], "response_metadata": {"next_cursor": "more"}}

    with pytest.raises(SlackReadRefusedError, match="page_cap_exceeded"):
        read_for_write(get, token="xoxp-user", channel="C1", ts="reply", thread_ts="root")


def test_mutations_route_token_and_clear_old_blocks() -> None:
    calls: list[tuple[str, dict[str, object], str, bool]] = []

    def post(method: str, payload: dict[str, object], *, token: str, idempotent: bool = True) -> RawAPIDict:
        calls.append((method, payload, token, idempotent))
        return {"ok": True}

    assert update_routed(post, token="", channel="C1", ts="1.0", text="new") == {}
    assert delete_routed(post, token="", channel="C1", ts="1.0") == {}
    assert update_routed(post, token="xoxp", channel="C1", ts="1.0", text="new") == {"ok": True}
    assert delete_routed(post, token="xoxp", channel="C1", ts="1.0") == {"ok": True}
    assert calls == [
        ("chat.update", {"channel": "C1", "ts": "1.0", "text": "new", "blocks": []}, "xoxp", True),
        ("chat.delete", {"channel": "C1", "ts": "1.0"}, "xoxp", False),
    ]


def test_update_wraps_text_before_the_wire_call() -> None:
    payloads: list[dict[str, object]] = []

    def post(method: str, payload: dict[str, object], *, token: str) -> RawAPIDict:
        assert method == "chat.update"
        assert token == "xoxp"
        payloads.append(payload)
        return {"ok": True}

    text = "A long sentence that should wrap before this update reaches Slack, preserving its natural word breaks."
    update_routed(post, token="xoxp", channel="C1", ts="1.0", text=text)
    assert payloads[0]["text"] == wrap_slack_message(text)
