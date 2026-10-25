"""Branch coverage for the dm_history single-message and thread-reply readers and the page walker.

The raw ``conversations.*`` read helpers split out of ``SlackBotBackend``
to keep the backend under the module-health LOC cap (#2061). Pure functions
over an injected ``get`` callable — no Slack network, no Django.
"""

import logging

import pytest

from teatree.backends.slack.bot_errors import SlackReadRefusedError
from teatree.backends.slack.dm_history import (
    _MAX_THREAD_PAGES,
    PageLimit,
    read_single_message,
    read_thread_replies,
    walk_pages,
)
from teatree.types import RawAPIDict


def _ok(messages: list[RawAPIDict]) -> RawAPIDict:
    return {"ok": True, "messages": messages}


def _page(messages: list[RawAPIDict], next_cursor: str = "") -> RawAPIDict:
    data = _ok(messages)
    if next_cursor:
        data["response_metadata"] = {"next_cursor": next_cursor}
    return data


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

    def test_strict_read_refuses_instead_of_reporting_absence(self) -> None:
        with pytest.raises(SlackReadRefusedError, match="not_in_channel"):
            read_single_message(
                get=lambda *_: {"ok": False, "error": "not_in_channel"}, channel="C1", ts="1.0", refuse=True
            )

    def test_strict_read_does_not_mistake_previous_message_for_deleted_target(self) -> None:
        message = read_single_message(
            get=lambda *_: _ok([{"ts": "0.9", "text": "previous"}]), channel="C1", ts="1.0", refuse=True
        )
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

    def test_a_refused_read_raises_the_slack_error_never_an_empty_thread(self) -> None:
        refusal: RawAPIDict = {"ok": False, "error": "thread_not_found"}

        with pytest.raises(SlackReadRefusedError, match="thread_not_found") as excinfo:
            read_thread_replies(get=lambda *_, **__: refusal, channel="D1", thread_ts="root.ts")

        assert excinfo.value.error_code == "thread_not_found"

    def test_an_ok_empty_thread_returns_empty(self) -> None:
        assert read_thread_replies(get=lambda *_, **__: _ok([]), channel="D1", thread_ts="root.ts") == []

    def test_follows_the_cursor_across_pages(self) -> None:
        pages = {
            "": _page([{"ts": "root.ts"}, {"ts": "r1"}], next_cursor="c1"),
            "c1": _page([{"ts": "r2"}, {"ts": "r3"}], next_cursor="c2"),
            "c2": _page([{"ts": "r4"}]),
        }

        def get(method: str, params: dict[str, str | int]) -> RawAPIDict:
            assert method == "conversations.replies"
            return pages[str(params.get("cursor", ""))]

        replies = read_thread_replies(get=get, channel="D1", thread_ts="root.ts")

        assert [r["ts"] for r in replies] == ["root.ts", "r1", "r2", "r3", "r4"]
        assert all(r["channel"] == "D1" for r in replies)

    def test_page_cap_logs_and_stops(self, caplog: pytest.LogCaptureFixture) -> None:
        def get(method: str, params: dict[str, str | int]) -> RawAPIDict:
            return _page([{"ts": str(params.get("cursor", "start"))}], next_cursor="more")

        with caplog.at_level(logging.WARNING):
            replies = read_thread_replies(get=get, channel="D1", thread_ts="root.ts")

        assert len(replies) == _MAX_THREAD_PAGES
        assert "hit the" in caplog.text

    def test_a_refused_page_mid_walk_raises_rather_than_returning_half_a_thread(self) -> None:
        """Half a thread reads as "my answer is not there", and the caller re-posts it."""
        pages: dict[str, RawAPIDict] = {
            "": _page([{"ts": "root.ts"}, {"ts": "r1"}], next_cursor="c1"),
            "c1": {"ok": False, "error": "ratelimited"},
        }

        def get(_method: str, params: dict[str, str | int]) -> RawAPIDict:
            return pages[str(params.get("cursor", ""))]

        with pytest.raises(SlackReadRefusedError, match="ratelimited"):
            read_thread_replies(get=get, channel="D1", thread_ts="root.ts")


class _TokenedThread:
    """A ``conversations.replies`` stub answering per token, recording which tokens asked."""

    def __init__(self, by_token: dict[str, RawAPIDict]) -> None:
        self._by_token = by_token
        self.tokens: list[str] = []

    def __call__(self, method: str, params: dict[str, str | int], *, token: str = "") -> RawAPIDict:
        assert method == "conversations.replies"
        assert params["ts"] == "root.ts"
        self.tokens.append(token)
        return self._by_token[token]


class TestReadThreadRepliesUserTokenFallback:
    @pytest.mark.parametrize("bot_error", ["not_in_channel", "channel_not_found"])
    def test_a_thread_the_bot_cannot_see_is_read_through_the_user_token(self, bot_error: str) -> None:
        get = _TokenedThread(
            {
                "": {"ok": False, "error": bot_error},
                "xoxp-user": _ok([{"ts": "root.ts"}, {"ts": "r1"}]),
            }
        )

        replies = read_thread_replies(get=get, channel="C1", thread_ts="root.ts", user_token="xoxp-user")

        assert [r["ts"] for r in replies] == ["root.ts", "r1"]
        assert all(r["channel"] == "C1" for r in replies)
        assert get.tokens == ["", "xoxp-user"]

    def test_both_tokens_refused_raises_the_user_token_error(self) -> None:
        get = _TokenedThread(
            {
                "": {"ok": False, "error": "not_in_channel"},
                "xoxp-user": {"ok": False, "error": "channel_not_found"},
            }
        )

        with pytest.raises(SlackReadRefusedError, match="channel_not_found") as excinfo:
            read_thread_replies(get=get, channel="C1", thread_ts="root.ts", user_token="xoxp-user")

        assert isinstance(excinfo.value.__context__, SlackReadRefusedError)
        assert excinfo.value.__context__.error_code == "not_in_channel"

    def test_a_refusal_the_user_token_cannot_cure_is_not_retried(self) -> None:
        get = _TokenedThread({"": {"ok": False, "error": "ratelimited"}})

        with pytest.raises(SlackReadRefusedError, match="ratelimited"):
            read_thread_replies(get=get, channel="C1", thread_ts="root.ts", user_token="xoxp-user")

        assert get.tokens == [""]

    def test_without_a_user_token_the_bot_refusal_is_raised(self) -> None:
        get = _TokenedThread({"": {"ok": False, "error": "not_in_channel"}})

        with pytest.raises(SlackReadRefusedError, match="not_in_channel"):
            read_thread_replies(get=get, channel="C1", thread_ts="root.ts")

        assert get.tokens == [""]


class TestWalkPages:
    def test_every_page_repeats_the_params_and_follows_the_cursor(self) -> None:
        pages = {
            "": _page([{"ts": "3.0"}, {"ts": "2.9"}], next_cursor="c1"),
            "c1": _page([{"ts": "2.8"}]),
        }
        seen: list[dict[str, str | int]] = []

        def get(method: str, params: dict[str, str | int]) -> RawAPIDict:
            assert method == "conversations.history"
            seen.append(params)
            return pages[str(params.get("cursor", ""))]

        messages = walk_pages(
            get, "conversations.history", {"channel": "D1", "oldest": "2.0"}, limit=PageLimit(5, "DM channel D1")
        )

        assert [m["ts"] for m in messages] == ["3.0", "2.9", "2.8"]
        assert seen == [{"channel": "D1", "oldest": "2.0"}, {"channel": "D1", "oldest": "2.0", "cursor": "c1"}]

    def test_a_refused_page_raises_naming_the_method_subject_and_error(self) -> None:
        pages: dict[str, RawAPIDict] = {
            "": _page([{"ts": "3.0"}], next_cursor="c1"),
            "c1": {"ok": False, "error": "ratelimited"},
        }

        def get(_method: str, params: dict[str, str | int]) -> RawAPIDict:
            return pages[str(params.get("cursor", ""))]

        with pytest.raises(SlackReadRefusedError, match="ratelimited") as excinfo:
            walk_pages(get, "conversations.history", {"channel": "D1"}, limit=PageLimit(5, "DM channel D1"))

        refused = excinfo.value
        assert (refused.method, refused.subject, refused.error_code) == (
            "conversations.history",
            "DM channel D1",
            "ratelimited",
        )

    def test_the_cap_logs_the_truncation_and_returns_what_was_read(self, caplog: pytest.LogCaptureFixture) -> None:
        def get(_method: str, params: dict[str, str | int]) -> RawAPIDict:
            return _page([{"ts": str(params.get("cursor", "start"))}], next_cursor="more")

        with caplog.at_level(logging.WARNING):
            messages = walk_pages(get, "conversations.history", {"channel": "D1"}, limit=PageLimit(3, "DM channel D1"))

        assert [m["ts"] for m in messages] == ["start", "more", "more"]
        assert "conversations.history hit the 3-page cap on DM channel D1" in caplog.text

    def test_strict_cap_refuses_the_partial_read(self) -> None:
        def get(_method: str, _params: dict[str, str | int]) -> RawAPIDict:
            return _page([{"ts": "3.0"}], next_cursor="more")

        with pytest.raises(SlackReadRefusedError) as refused:
            walk_pages(
                get, "conversations.history", {"channel": "D1"}, limit=PageLimit(2, "DM channel D1"), strict=True
            )

        assert refused.value.error_code == "page_cap_reached"
