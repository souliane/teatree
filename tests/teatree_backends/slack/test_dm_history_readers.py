"""Branch coverage for the dm_history single-message and thread-reply readers.

The two raw ``conversations.*`` read helpers split out of ``SlackBotBackend``
to keep the backend under the module-health LOC cap (#2061). Pure functions
over an injected ``get`` callable — no Slack network, no Django.
"""

from teatree.backends.slack.dm_history import read_single_message, read_thread_replies
from teatree.types import RawAPIDict


def _ok(messages: list[RawAPIDict]) -> RawAPIDict:
    return {"ok": True, "messages": messages}


class TestReadSingleMessage:
    def test_returns_first_message_with_channel_stamped(self) -> None:
        def get(method: str, params: dict[str, str | int]) -> RawAPIDict:
            assert method == "conversations.history"
            assert params["latest"] == "1.0"
            return _ok([{"ts": "1.0", "text": "hi"}])

        message = read_single_message(get=get, channel="D1", ts="1.0")

        assert message == {"ts": "1.0", "text": "hi", "channel": "D1"}

    def test_empty_channel_or_ts_short_circuits(self) -> None:
        calls: list[str] = []

        def get(method: str, params: dict[str, str | int]) -> RawAPIDict:
            calls.append(method)
            return {}

        assert read_single_message(get=get, channel="", ts="1.0") == {}
        assert read_single_message(get=get, channel="D1", ts="") == {}
        assert calls == []  # never hit Slack on an empty key

    def test_non_ok_response_returns_empty(self) -> None:
        message = read_single_message(get=lambda *_: {"ok": False}, channel="D1", ts="1.0")
        assert message == {}

    def test_no_matching_message_returns_empty(self) -> None:
        message = read_single_message(get=lambda *_: _ok([]), channel="D1", ts="1.0")
        assert message == {}

    def test_does_not_clobber_an_existing_channel_field(self) -> None:
        message = read_single_message(get=lambda *_: _ok([{"ts": "1.0", "channel": "ALREADY"}]), channel="D1", ts="1.0")
        assert message["channel"] == "ALREADY"


class TestReadThreadReplies:
    def test_returns_every_reply_with_channel_stamped(self) -> None:
        def get(method: str, params: dict[str, str | int]) -> RawAPIDict:
            assert method == "conversations.replies"
            assert params["ts"] == "root.ts"
            return _ok([{"ts": "root.ts", "text": "q"}, {"ts": "r1", "text": "a"}])

        replies = read_thread_replies(get=get, channel="D1", thread_ts="root.ts")

        assert [r["ts"] for r in replies] == ["root.ts", "r1"]
        assert all(r["channel"] == "D1" for r in replies)

    def test_empty_channel_or_root_short_circuits(self) -> None:
        calls: list[str] = []

        def get(method: str, params: dict[str, str | int]) -> RawAPIDict:
            calls.append(method)
            return {}

        assert read_thread_replies(get=get, channel="", thread_ts="root.ts") == []
        assert read_thread_replies(get=get, channel="D1", thread_ts="") == []
        assert calls == []  # never hit Slack on an empty key

    def test_non_ok_response_returns_empty(self) -> None:
        assert read_thread_replies(get=lambda *_: {"ok": False}, channel="D1", thread_ts="root.ts") == []
