"""Backend read chokepoint strips the bot's OWN TTS audio (teatree#2089).

The Slack-TTS feature attaches a synthesised ``speech.m4a`` to the bot's own
DM messages. When the loop reads Slack history (``fetch_message``,
``fetch_channel_history``, ``fetch_dms`` thread replies) the bot's own audio
attachment must NOT be surfaced to the agent — re-transcribing a spoken copy
of text the bot already wrote is pure token waste. Audio on messages authored
by OTHERS (the user's voice notes) must still flow through.

These drive the real :class:`SlackBotBackend` read methods through a mocked
Slack HTTP transport, so the test proves the chokepoint strips at the backend
boundary — reverting ``_strip_own_tts_audio`` turns them red.
"""

from typing import cast

import httpx
import pytest

from teatree.backends.slack import http as slack_http
from teatree.backends.slack.bot import SlackBotBackend

_OWN_USER_ID = "U_BOT_SELF"
_OWN_BOT_ID = "B_BOT_SELF"
_USER_ID = "U_HUMAN"
_CHANNEL = "D0000000001"


def _tts_file() -> dict[str, object]:
    return {
        "id": "F0SPEECH",
        "name": "speech.m4a",
        "mimetype": "audio/mp4",
        "filetype": "m4a",
        "url_private": "https://files.slack.com/files-pri/T/F0SPEECH/speech.m4a",
    }


def _user_voice_note() -> dict[str, object]:
    return {
        "id": "F0VOICE",
        "name": "audio_message.m4a",
        "mimetype": "audio/mp4",
        "filetype": "m4a",
        "url_private": "https://files.slack.com/files-pri/T/F0VOICE/audio_message.m4a",
    }


def _install_transport(
    monkeypatch: pytest.MonkeyPatch,
    *,
    history_messages: list[dict[str, object]],
) -> None:
    """Mock Slack: ``auth.test`` resolves the bot ids, history returns *history_messages*."""

    def fake_post(url: str, **kwargs: object) -> httpx.Response:
        if url.endswith("/auth.test"):
            return httpx.Response(
                200,
                json={"ok": True, "user_id": _OWN_USER_ID, "bot_id": _OWN_BOT_ID},
                request=httpx.Request("POST", url),
            )
        return httpx.Response(200, json={"ok": True}, request=httpx.Request("POST", url))

    def fake_get(url: str, **kwargs: object) -> httpx.Response:
        _ = kwargs
        if url.endswith("/conversations.history"):
            return httpx.Response(
                200,
                json={"ok": True, "messages": history_messages},
                request=httpx.Request("GET", url),
            )
        return httpx.Response(200, json={"ok": True}, request=httpx.Request("GET", url))

    monkeypatch.setattr(slack_http.httpx, "post", fake_post)
    monkeypatch.setattr(slack_http.httpx, "get", fake_get)


class TestFetchMessageStripsOwnAudio:
    def test_bot_own_message_audio_stripped(self, monkeypatch: pytest.MonkeyPatch) -> None:
        _install_transport(
            monkeypatch,
            history_messages=[
                {
                    "ts": "1.0",
                    "user": _OWN_USER_ID,
                    "text": "PR merged, shipping now",
                    "files": [_tts_file()],
                }
            ],
        )
        backend = SlackBotBackend(bot_token="xoxb-bot")

        message = backend.fetch_message(channel=_CHANNEL, ts="1.0")

        assert message["text"] == "PR merged, shipping now"
        assert message.get("files", []) == []

    def test_user_voice_note_audio_preserved(self, monkeypatch: pytest.MonkeyPatch) -> None:
        _install_transport(
            monkeypatch,
            history_messages=[
                {
                    "ts": "2.0",
                    "user": _USER_ID,
                    "text": "",
                    "files": [_user_voice_note()],
                }
            ],
        )
        backend = SlackBotBackend(bot_token="xoxb-bot")

        message = backend.fetch_message(channel=_CHANNEL, ts="2.0")

        files = cast("list[dict[str, object]]", message.get("files", []))
        assert len(files) == 1
        assert files[0]["id"] == "F0VOICE"


class TestFetchChannelHistoryStripsOwnAudio:
    def test_bot_own_audio_stripped_user_voice_kept(self, monkeypatch: pytest.MonkeyPatch) -> None:
        _install_transport(
            monkeypatch,
            history_messages=[
                {"ts": "1.0", "user": _OWN_USER_ID, "text": "bot status", "files": [_tts_file()]},
                {"ts": "2.0", "user": _USER_ID, "text": "voice", "files": [_user_voice_note()]},
            ],
        )
        backend = SlackBotBackend(bot_token="xoxb-bot")

        messages = backend.fetch_channel_history(channel="C0PUBLIC", limit=10)

        by_ts = {m["ts"]: m for m in messages}
        assert by_ts["1.0"].get("files", []) == []
        user_files = cast("list[dict[str, object]]", by_ts["2.0"].get("files", []))
        assert len(user_files) == 1
        assert user_files[0]["id"] == "F0VOICE"


class TestFailsOpenWhenIdentityUnresolved:
    def test_bot_audio_kept_when_auth_test_not_ok(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Identity unresolvable → fail open: the read is left unchanged."""

        def fake_post(url: str, **kwargs: object) -> httpx.Response:
            if url.endswith("/auth.test"):
                return httpx.Response(
                    200, json={"ok": False, "error": "invalid_auth"}, request=httpx.Request("POST", url)
                )
            return httpx.Response(200, json={"ok": True}, request=httpx.Request("POST", url))

        def fake_get(url: str, **kwargs: object) -> httpx.Response:
            _ = kwargs
            return httpx.Response(
                200,
                json={"ok": True, "messages": [{"ts": "1.0", "user": _OWN_USER_ID, "files": [_tts_file()]}]},
                request=httpx.Request("GET", url),
            )

        monkeypatch.setattr(slack_http.httpx, "post", fake_post)
        monkeypatch.setattr(slack_http.httpx, "get", fake_get)
        backend = SlackBotBackend(bot_token="xoxb-bot")

        message = backend.fetch_message(channel=_CHANNEL, ts="1.0")

        files = cast("list[dict[str, object]]", message.get("files", []))
        assert len(files) == 1
