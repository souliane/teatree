"""``SlackBotBackend.post_audio_dm`` — the #2050 attach-audio-to-the-DM egress.

The modern three-step Slack upload (``files.getUploadURLExternal`` →
binary POST to the off-Slack upload URL → ``files.completeUploadExternal``
sharing into the DM channel) — now with ``initial_comment`` (the DM text
above the file) and ``thread_ts`` on the final call so the result is ONE DM
message carrying the text + an inline audio player (#2050). Only the Slack
HTTP boundary (``httpx.get`` / ``httpx.post``) is mocked; the routing, the
file read, the scope-failure surfacing, and the missing-file / empty-token
guards run against the real backend.

The file transfer is a POST per ``files.getUploadURLExternal``: Slack's
file storage host accepts the bytes only on POST and 302-redirects any
other verb to ``slack.com``, so a PUT here silently dropped the audio.
"""

from pathlib import Path
from typing import cast

import httpx
import pytest

from teatree.backends.slack.bot import SlackBotBackend

_SELF_DM = "D_SELF"
_API_HOST = "slack.com/api"


def _backend() -> SlackBotBackend:
    return SlackBotBackend(bot_token="xoxb-bot", user_token="xoxp-user", user_id="U_ME", dm_channel_id=_SELF_DM)


@pytest.fixture
def audio_file(tmp_path: Path) -> Path:
    path = tmp_path / "speech.m4a"
    path.write_bytes(b"fake-audio-bytes")
    return path


class TestPostAudioDm:
    def test_full_flow_sets_initial_comment_and_thread_ts(
        self,
        audio_file: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        get_calls: list[tuple[str, dict[str, object]]] = []
        upload_calls: list[tuple[str, bytes, str | None]] = []
        complete_calls: list[tuple[str, dict[str, object], str]] = []

        def fake_get(url: str, **kwargs: object) -> httpx.Response:
            params = cast("dict[str, object]", kwargs.get("params") or {})
            get_calls.append((url, params))
            body = {"ok": True, "upload_url": "https://files.slack/upload/abc", "file_id": "F123"}
            return httpx.Response(200, json=body, request=httpx.Request("GET", url))

        def fake_post(url: str, **kwargs: object) -> httpx.Response:
            headers = cast("dict[str, str]", kwargs.get("headers") or {})
            if _API_HOST not in url:
                upload_calls.append((url, cast("bytes", kwargs.get("content", b"")), headers.get("Authorization")))
                return httpx.Response(200, request=httpx.Request("POST", url))
            payload = cast("dict[str, object]", kwargs.get("json") or {})
            complete_calls.append((url, payload, headers["Authorization"]))
            return httpx.Response(200, json={"ok": True, "files": [{"id": "F123"}]}, request=httpx.Request("POST", url))

        monkeypatch.setattr(httpx, "get", fake_get)
        monkeypatch.setattr(httpx, "post", fake_post)

        result = _backend().post_audio_dm(
            channel=_SELF_DM,
            filepath=str(audio_file),
            text="tests are green",
            thread_ts="1700.0001",
            title="Agent reply",
        )

        assert result["ok"] is True
        # Step 1: reserve with filename + length on the bot token (self-DM → bot).
        assert get_calls[0][0].endswith("files.getUploadURLExternal")
        assert get_calls[0][1]["filename"] == "speech.m4a"
        assert get_calls[0][1]["length"] == len(b"fake-audio-bytes")
        # Step 2: POST the exact bytes to the reserved URL, untokened.
        assert upload_calls == [("https://files.slack/upload/abc", b"fake-audio-bytes", None)]
        # Step 3: complete + share with the text as initial_comment, threaded — ONE DM.
        assert complete_calls[0][0].endswith("files.completeUploadExternal")
        complete_payload = complete_calls[0][1]
        assert complete_payload["channel_id"] == _SELF_DM
        assert complete_payload["initial_comment"] == "tests are green"
        assert complete_payload["thread_ts"] == "1700.0001"
        assert complete_payload["files"] == [{"id": "F123", "title": "Agent reply"}]
        assert complete_calls[0][2] == "Bearer xoxb-bot"

    def test_no_thread_ts_omits_the_field(self, audio_file: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        complete_payloads: list[dict[str, object]] = []

        def fake_get(url: str, **kwargs: object) -> httpx.Response:
            body = {"ok": True, "upload_url": "https://files.slack/u", "file_id": "F1"}
            return httpx.Response(200, json=body, request=httpx.Request("GET", url))

        def fake_post(url: str, **kwargs: object) -> httpx.Response:
            if _API_HOST not in url:
                return httpx.Response(200, request=httpx.Request("POST", url))
            complete_payloads.append(cast("dict[str, object]", kwargs.get("json") or {}))
            return httpx.Response(200, json={"ok": True}, request=httpx.Request("POST", url))

        monkeypatch.setattr(httpx, "get", fake_get)
        monkeypatch.setattr(httpx, "post", fake_post)

        _backend().post_audio_dm(channel=_SELF_DM, filepath=str(audio_file), text="hi")
        assert "thread_ts" not in complete_payloads[0]
        assert complete_payloads[0]["initial_comment"] == "hi"

    def test_empty_when_no_token_configured(self, audio_file: Path) -> None:
        backend = SlackBotBackend(bot_token="", user_token="", user_id="U_ME")
        assert backend.post_audio_dm(channel=_SELF_DM, filepath=str(audio_file), text="hi") == {}

    def test_empty_when_channel_blank(self, audio_file: Path) -> None:
        assert _backend().post_audio_dm(channel="", filepath=str(audio_file), text="hi") == {}

    def test_empty_when_file_missing(self, tmp_path: Path) -> None:
        missing = tmp_path / "nope.m4a"
        assert _backend().post_audio_dm(channel=_SELF_DM, filepath=str(missing), text="hi") == {}

    def test_returns_reserve_body_on_missing_scope(
        self,
        audio_file: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        def fake_get(url: str, **kwargs: object) -> httpx.Response:
            return httpx.Response(
                200,
                json={"ok": False, "error": "missing_scope", "needed": "files:write"},
                request=httpx.Request("GET", url),
            )

        monkeypatch.setattr(httpx, "get", fake_get)
        result = _backend().post_audio_dm(channel=_SELF_DM, filepath=str(audio_file), text="hi")
        assert result["ok"] is False
        assert result["error"] == "missing_scope"
        assert result["needed"] == "files:write"

    def test_returns_reserve_body_when_url_or_id_missing(
        self,
        audio_file: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        def fake_get(url: str, **kwargs: object) -> httpx.Response:
            return httpx.Response(200, json={"ok": True}, request=httpx.Request("GET", url))

        monkeypatch.setattr(httpx, "get", fake_get)
        result = _backend().post_audio_dm(channel=_SELF_DM, filepath=str(audio_file), text="hi")
        assert result == {"ok": True}

    def test_no_title_omits_title_field(
        self,
        audio_file: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        complete_payloads: list[dict[str, object]] = []

        def fake_get(url: str, **kwargs: object) -> httpx.Response:
            body = {"ok": True, "upload_url": "https://files.slack/u", "file_id": "F1"}
            return httpx.Response(200, json=body, request=httpx.Request("GET", url))

        def fake_post(url: str, **kwargs: object) -> httpx.Response:
            if _API_HOST not in url:
                return httpx.Response(200, request=httpx.Request("POST", url))
            complete_payloads.append(cast("dict[str, object]", kwargs.get("json") or {}))
            return httpx.Response(200, json={"ok": True}, request=httpx.Request("POST", url))

        monkeypatch.setattr(httpx, "get", fake_get)
        monkeypatch.setattr(httpx, "post", fake_post)

        _backend().post_audio_dm(channel=_SELF_DM, filepath=str(audio_file), text="hi")
        assert complete_payloads[0]["files"] == [{"id": "F1"}]


class TestPostExternal:
    def test_posts_bytes_untokened_and_returns_status(self, monkeypatch: pytest.MonkeyPatch) -> None:
        from teatree.backends.slack.http import SlackHttpClient  # noqa: PLC0415

        seen: list[tuple[str, bytes, str | None]] = []

        def fake_post(url: str, **kwargs: object) -> httpx.Response:
            headers = cast("dict[str, str]", kwargs.get("headers") or {})
            seen.append((url, cast("bytes", kwargs.get("content", b"")), headers.get("Authorization")))
            return httpx.Response(200, request=httpx.Request("POST", url))

        monkeypatch.setattr(httpx, "post", fake_post)
        client = SlackHttpClient(sleep=lambda _: None)
        assert client.post_external("https://files.slack/u", content=b"x") == 200
        assert seen == [("https://files.slack/u", b"x", None)]
