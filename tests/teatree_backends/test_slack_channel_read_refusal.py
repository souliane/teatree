"""A channel the bot cannot read must SAY so, never answer ``[]``.

``conversations.history`` answers ``not_in_channel`` / ``channel_not_found`` for
every channel the bot was not invited to — the common case, not an edge case. The
scan loop swallows that to ``[]`` on purpose (one unreadable channel must not break
a poll over many), but the same swallow on the interactive single-channel read told
an agent "this channel is quiet" while meaning "I cannot see this channel at all".
"""

from unittest.mock import patch

import pytest

from teatree.backends.messaging_noop import NoopMessagingBackend
from teatree.backends.slack.bot import SlackBotBackend
from teatree.backends.slack.routing import OwnerDmOnlyError
from teatree.backends.slack.web_reads import read_channel_history, read_channel_history_or_refuse
from teatree.types import ChannelReadRefusedError, RawAPIDict, ScannerError


def _getter(response: RawAPIDict):
    def _get(method: str, params: dict[str, str | int], *, token: str = "") -> RawAPIDict:
        _ = (method, params, token)
        return response

    return _get


_REFUSALS = ("not_in_channel", "channel_not_found", "is_archived")


class _TokenedHttp:
    def __init__(self, by_token: dict[str, RawAPIDict]) -> None:
        self.by_token = by_token
        self.history_tokens: list[str] = []

    def get(self, method: str, *, token: str, params: dict[str, str | int]) -> RawAPIDict:
        if method == "auth.test":
            return {"ok": False, "error": "unknown"}
        assert method == "conversations.history"
        assert params["channel"] in {"D1", "C1"}
        self.history_tokens.append(token)
        return self.by_token[token]


def _backend(fake: _TokenedHttp, *, user_token: str | None = None, owner_dm_only: bool = False) -> SlackBotBackend:
    with patch("teatree.backends.slack.bot.SlackHttpClient", return_value=fake):
        return SlackBotBackend(
            bot_token="xoxb-bot",
            user_token="xoxp-user" if user_token is None else user_token,
            user_id="UOWNER",
            dm_channel_id="DOWNER",
            owner_dm_only=owner_dm_only,
        )


def test_both_history_tokens_refused_keeps_the_bot_error_as_context() -> None:
    fake = _TokenedHttp(
        {
            "xoxb-bot": {"ok": False, "error": "not_in_channel"},
            "xoxp-user": {"ok": False, "error": "channel_not_found"},
        }
    )

    with pytest.raises(ChannelReadRefusedError) as refused:
        _backend(fake).fetch_channel_history_or_refuse(channel="D1")

    assert refused.value.error_code == "channel_not_found"
    assert isinstance(refused.value.__context__, ChannelReadRefusedError)
    assert refused.value.__context__.error_code == "not_in_channel"
    assert "/invite" not in str(refused.value)
    assert fake.history_tokens == ["xoxb-bot", "xoxp-user"]


@pytest.mark.parametrize(
    ("error_code", "expected_error"),
    [("is_archived", ChannelReadRefusedError), ("invalid_auth", ScannerError)],
)
def test_other_history_refusals_do_not_retry(error_code: str, expected_error: type[Exception]) -> None:
    fake = _TokenedHttp({"xoxb-bot": {"ok": False, "error": error_code}})

    with pytest.raises(expected_error):
        _backend(fake).fetch_channel_history_or_refuse(channel="D1")

    assert fake.history_tokens == ["xoxb-bot"]


def test_history_does_not_retry_without_a_user_token() -> None:
    fake = _TokenedHttp({"xoxb-bot": {"ok": False, "error": "channel_not_found"}})

    with pytest.raises(ChannelReadRefusedError):
        _backend(fake, user_token="").fetch_channel_history_or_refuse(channel="D1")

    assert fake.history_tokens == ["xoxb-bot"]


def test_readable_dm_and_non_dm_keep_their_first_token() -> None:
    fake = _TokenedHttp(
        {
            "xoxb-bot": {"ok": True, "messages": [{"ts": "1.0"}]},
            "xoxp-user": {"ok": True, "messages": [{"ts": "2.0"}]},
        }
    )
    backend = _backend(fake)

    assert backend.fetch_channel_history_or_refuse(channel="D1") == [{"ts": "1.0", "channel": "D1"}]
    assert backend.fetch_channel_history_or_refuse(channel="C1") == [{"ts": "2.0", "channel": "C1"}]
    assert fake.history_tokens == ["xoxb-bot", "xoxp-user"]


def test_non_dm_user_token_refusal_is_not_retried() -> None:
    fake = _TokenedHttp({"xoxp-user": {"ok": False, "error": "not_in_channel"}})

    with pytest.raises(ChannelReadRefusedError):
        _backend(fake).fetch_channel_history_or_refuse(channel="C1")

    assert fake.history_tokens == ["xoxp-user"]


def test_owner_dm_guard_refuses_before_any_history_call() -> None:
    fake = _TokenedHttp({})

    with pytest.raises(OwnerDmOnlyError):
        _backend(fake, owner_dm_only=True).fetch_channel_history_or_refuse(channel="D1")

    assert fake.history_tokens == []


@pytest.mark.parametrize("error_code", _REFUSALS)
def test_the_interactive_read_names_the_refusal(error_code: str) -> None:
    with pytest.raises(ChannelReadRefusedError) as refused:
        read_channel_history_or_refuse(
            get=_getter({"ok": False, "error": error_code}),
            channel="#sample-channel",
            token="xoxb-x",
        )
    assert refused.value.error_code == error_code
    assert "NOT an empty channel" in str(refused.value)
    assert "invited" in str(refused.value)


@pytest.mark.parametrize("error_code", _REFUSALS)
def test_the_scan_loop_read_still_swallows_so_one_channel_never_breaks_a_poll(error_code: str) -> None:
    assert (
        read_channel_history(
            get=_getter({"ok": False, "error": error_code}),
            channel="#sample-channel",
            token="xoxb-x",
        )
        == []
    )


def test_a_genuinely_empty_channel_is_still_an_empty_list_not_a_refusal() -> None:
    assert (
        read_channel_history_or_refuse(
            get=_getter({"ok": True, "messages": []}),
            channel="#quiet",
            token="xoxb-x",
        )
        == []
    )


def test_a_global_token_failure_still_raises_the_scanner_error_not_a_channel_refusal() -> None:
    with pytest.raises(ScannerError):
        read_channel_history_or_refuse(
            get=_getter({"ok": False, "error": "invalid_auth"}),
            channel="#sample-channel",
            token="xoxb-x",
        )


def test_the_noop_backend_refuses_rather_than_pretending_a_channel_is_empty() -> None:
    with pytest.raises(ChannelReadRefusedError):
        NoopMessagingBackend.fetch_channel_history_or_refuse(channel="#sample-channel")


def test_backend_method_refuses_an_empty_channel() -> None:
    backend = SlackBotBackend(bot_token="xoxb-test-token")
    with pytest.raises(ChannelReadRefusedError) as refused:
        backend.fetch_channel_history_or_refuse(channel="")
    assert refused.value.error_code == "empty_channel_argument"


def test_backend_method_reads_and_strips_on_a_named_channel() -> None:
    backend = SlackBotBackend(bot_token="xoxb-test-token")
    messages: list[RawAPIDict] = [{"text": "hi"}]
    with (
        patch.object(backend, "_channel_token", return_value="tok"),
        patch("teatree.backends.slack.bot.read_channel_history_or_refuse", return_value=messages),
        patch.object(backend, "_strip_own_tts_audio", side_effect=lambda m: m),
    ):
        assert backend.fetch_channel_history_or_refuse(channel="#sample-channel") == messages
