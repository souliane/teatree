"""``SlackBotBackend.upload_audio_to_dm`` — the #1791 ``slack-audio`` egress.

The modern three-step Slack upload (``files.getUploadURLExternal`` →
binary PUT to the off-Slack upload URL → ``files.completeUploadExternal``
sharing into the DM channel). Only the Slack HTTP boundary
(``httpx.get`` / ``httpx.put`` / ``httpx.post``) is mocked; the routing,
the file read, the ``files:write`` scope-failure surfacing, and the
missing-file / empty-token guards are exercised against the real backend.
"""

from pathlib import Path
from typing import cast

import httpx
import pytest

from teatree.backends.slack_bot import SlackBotBackend

_SELF_DM = "D_SELF"


def _backend() -> SlackBotBackend:
    return SlackBotBackend(bot_token="xoxb-bot", user_token="xoxp-user", user_id="U_ME", dm_channel_id=_SELF_DM)


@pytest.fixture
def audio_file(tmp_path: Path) -> Path:
    path = tmp_path / "speech.m4a"
    path.write_bytes(b"fake-audio-bytes")
    return path


class TestUploadAudioToDm:
    def test_full_flow_reserves_puts_and_completes(
        self,
        audio_file: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        get_calls: list[tuple[str, dict[str, object]]] = []
        put_calls: list[tuple[str, bytes]] = []
        post_calls: list[tuple[str, dict[str, object], str]] = []

        def fake_get(url: str, **kwargs: object) -> httpx.Response:
            params = cast("dict[str, object]", kwargs.get("params") or {})
            get_calls.append((url, params))
            body = {"ok": True, "upload_url": "https://files.slack/upload/abc", "file_id": "F123"}
            return httpx.Response(200, json=body, request=httpx.Request("GET", url))

        def fake_put(url: str, **kwargs: object) -> httpx.Response:
            put_calls.append((url, cast("bytes", kwargs.get("content", b""))))
            return httpx.Response(200, request=httpx.Request("PUT", url))

        def fake_post(url: str, **kwargs: object) -> httpx.Response:
            headers = cast("dict[str, str]", kwargs["headers"])
            payload = cast("dict[str, object]", kwargs.get("json") or {})
            post_calls.append((url, payload, headers["Authorization"]))
            return httpx.Response(200, json={"ok": True, "files": [{"id": "F123"}]}, request=httpx.Request("POST", url))

        monkeypatch.setattr(httpx, "get", fake_get)
        monkeypatch.setattr(httpx, "put", fake_put)
        monkeypatch.setattr(httpx, "post", fake_post)

        result = _backend().upload_audio_to_dm(channel=_SELF_DM, filepath=str(audio_file), title="Agent reply")

        assert result["ok"] is True
        # Step 1: reserve with filename + length on the bot token (self-DM → bot).
        assert get_calls[0][0].endswith("files.getUploadURLExternal")
        assert get_calls[0][1]["filename"] == "speech.m4a"
        assert get_calls[0][1]["length"] == len(b"fake-audio-bytes")
        # Step 2: PUT the exact bytes to the reserved URL.
        assert put_calls == [("https://files.slack/upload/abc", b"fake-audio-bytes")]
        # Step 3: complete + share into the channel, on the bot token.
        assert post_calls[0][0].endswith("files.completeUploadExternal")
        assert post_calls[0][1]["channel_id"] == _SELF_DM
        assert post_calls[0][1]["files"] == [{"id": "F123", "title": "Agent reply"}]
        assert post_calls[0][2] == "Bearer xoxb-bot"

    def test_empty_when_no_token_configured(self, audio_file: Path) -> None:
        backend = SlackBotBackend(bot_token="", user_token="", user_id="U_ME")
        assert backend.upload_audio_to_dm(channel=_SELF_DM, filepath=str(audio_file)) == {}

    def test_empty_when_channel_blank(self, audio_file: Path) -> None:
        assert _backend().upload_audio_to_dm(channel="", filepath=str(audio_file)) == {}

    def test_empty_when_file_missing(self, tmp_path: Path) -> None:
        missing = tmp_path / "nope.m4a"
        assert _backend().upload_audio_to_dm(channel=_SELF_DM, filepath=str(missing)) == {}

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
        result = _backend().upload_audio_to_dm(channel=_SELF_DM, filepath=str(audio_file))
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
        result = _backend().upload_audio_to_dm(channel=_SELF_DM, filepath=str(audio_file))
        assert result == {"ok": True}

    def test_no_title_omits_title_field(
        self,
        audio_file: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        post_payloads: list[dict[str, object]] = []

        def fake_get(url: str, **kwargs: object) -> httpx.Response:
            body = {"ok": True, "upload_url": "https://files.slack/u", "file_id": "F1"}
            return httpx.Response(200, json=body, request=httpx.Request("GET", url))

        def fake_put(url: str, **kwargs: object) -> httpx.Response:
            return httpx.Response(200, request=httpx.Request("PUT", url))

        def fake_post(url: str, **kwargs: object) -> httpx.Response:
            post_payloads.append(cast("dict[str, object]", kwargs.get("json") or {}))
            return httpx.Response(200, json={"ok": True}, request=httpx.Request("POST", url))

        monkeypatch.setattr(httpx, "get", fake_get)
        monkeypatch.setattr(httpx, "put", fake_put)
        monkeypatch.setattr(httpx, "post", fake_post)

        _backend().upload_audio_to_dm(channel=_SELF_DM, filepath=str(audio_file))
        assert post_payloads[0]["files"] == [{"id": "F1"}]


class TestPutExternal:
    def test_returns_status_code(self, monkeypatch: pytest.MonkeyPatch) -> None:
        from teatree.backends.slack_http import SlackHttpClient  # noqa: PLC0415

        def fake_put(url: str, **kwargs: object) -> httpx.Response:
            return httpx.Response(200, request=httpx.Request("PUT", url))

        monkeypatch.setattr(httpx, "put", fake_put)
        client = SlackHttpClient(sleep=lambda _: None)
        assert client.put_external("https://files.slack/u", content=b"x") == 200
