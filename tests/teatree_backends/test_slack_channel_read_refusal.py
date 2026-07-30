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
from teatree.backends.slack.web_reads import read_channel_history, read_channel_history_or_refuse
from teatree.types import ChannelReadRefusedError, RawAPIDict, ScannerError


def _getter(response: RawAPIDict):
    def _get(method: str, params: dict[str, str | int], *, token: str = "") -> RawAPIDict:
        _ = (method, params, token)
        return response

    return _get


_REFUSALS = ("not_in_channel", "channel_not_found", "is_archived")


@pytest.mark.parametrize("error_code", _REFUSALS)
def test_the_interactive_read_names_the_refusal(error_code: str) -> None:
    with pytest.raises(ChannelReadRefusedError) as refused:
        read_channel_history_or_refuse(
            get=_getter({"ok": False, "error": error_code}),
            channel="#review-broadcasts",
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
            channel="#review-broadcasts",
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
            channel="#review-broadcasts",
            token="xoxb-x",
        )


def test_the_noop_backend_refuses_rather_than_pretending_a_channel_is_empty() -> None:
    with pytest.raises(ChannelReadRefusedError):
        NoopMessagingBackend.fetch_channel_history_or_refuse(channel="#review-broadcasts")


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
        assert backend.fetch_channel_history_or_refuse(channel="#review-broadcasts") == messages
