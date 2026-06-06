"""Tests for the #1750 Slack post/react destination router.

The binding routing rule is a *single deterministic destination test*,
identical for posting and reacting (``_route_token`` →
``post_routed`` / ``react_routed``): a message or reaction to the
user's *own DM* goes out under the per-overlay **bot** token, while a
message or reaction to a *colleague* or a *channel* goes out under the
user's personal **OAuth** (``xoxp-…``) token — never the
Connect-conditional ``_channel_token`` policy, which cannot tell the
user's own DM from a colleague's.

Only the Slack HTTP boundary (``httpx.post``) is mocked.
"""

from dataclasses import dataclass
from typing import cast

import httpx
import pytest

from teatree.backends.slack import http as slack_http
from teatree.backends.slack.bot import SlackBotBackend

_SELF_DM = "D_SELF"


@dataclass(frozen=True)
class _Call:
    url: str
    authorization: str
    json: dict[str, object]


def _capturing_post(captured: list[_Call], body: dict[str, object]) -> object:
    def fake_post(url: str, **kwargs: object) -> httpx.Response:
        headers = cast("dict[str, str]", kwargs["headers"])
        captured.append(
            _Call(url=url, authorization=headers["Authorization"], json=cast("dict[str, object]", kwargs["json"]))
        )
        return httpx.Response(200, json=body, request=httpx.Request("POST", url))

    return fake_post


def _backend() -> SlackBotBackend:
    return SlackBotBackend(bot_token="xoxb-bot", user_token="xoxp-user", user_id="U_ME", dm_channel_id=_SELF_DM)


class TestRouteToken:
    def test_public_route_token_matches_private(self) -> None:
        backend = _backend()
        assert backend.route_token(_SELF_DM) == "xoxb-bot"
        assert backend.route_token("C_TEAM") == "xoxp-user"

    def test_self_dm_channel_id_routes_to_bot(self) -> None:
        assert _backend()._route_token(_SELF_DM) == "xoxb-bot"

    def test_self_user_id_routes_to_bot(self) -> None:
        assert _backend()._route_token("U_ME") == "xoxb-bot"

    def test_colleague_dm_routes_to_user(self) -> None:
        assert _backend()._route_token("D_COLLEAGUE") == "xoxp-user"

    def test_channel_routes_to_user(self) -> None:
        assert _backend()._route_token("C_TEAM") == "xoxp-user"

    def test_empty_channel_routes_to_user(self) -> None:
        assert _backend()._route_token("") == "xoxp-user"

    def test_self_dm_falls_back_to_user_when_no_bot(self) -> None:
        backend = SlackBotBackend(user_token="xoxp-user", user_id="U_ME", dm_channel_id=_SELF_DM)
        assert backend._route_token(_SELF_DM) == "xoxp-user"

    def test_colleague_falls_back_to_bot_when_no_user(self) -> None:
        backend = SlackBotBackend(bot_token="xoxb-bot", user_id="U_ME", dm_channel_id=_SELF_DM)
        assert backend._route_token("C_TEAM") == "xoxb-bot"


class TestPostRouted:
    def test_self_dm_posts_under_bot_token(self, monkeypatch: pytest.MonkeyPatch) -> None:
        captured: list[_Call] = []
        monkeypatch.setattr(slack_http.httpx, "post", _capturing_post(captured, {"ok": True, "ts": "1.2"}))

        _backend().post_routed(channel=_SELF_DM, text="status update")

        assert captured[0].url.endswith("/chat.postMessage")
        assert captured[0].authorization == "Bearer xoxb-bot"

    def test_colleague_posts_under_user_token(self, monkeypatch: pytest.MonkeyPatch) -> None:
        captured: list[_Call] = []
        monkeypatch.setattr(slack_http.httpx, "post", _capturing_post(captured, {"ok": True, "ts": "1.2"}))

        result = _backend().post_routed(channel="D_COLLEAGUE", text="ping")

        assert result == {"ok": True, "ts": "1.2"}
        assert captured[0].authorization == "Bearer xoxp-user"

    def test_channel_posts_under_user_token_threaded(self, monkeypatch: pytest.MonkeyPatch) -> None:
        captured: list[_Call] = []
        monkeypatch.setattr(slack_http.httpx, "post", _capturing_post(captured, {"ok": True, "ts": "1.2"}))

        _backend().post_routed(channel="C_TEAM", text="hi team", thread_ts="1700.0001")

        assert captured[0].authorization == "Bearer xoxp-user"
        assert captured[0].json == {"channel": "C_TEAM", "text": "hi team", "thread_ts": "1700.0001"}

    def test_returns_error_body_on_not_ok(self, monkeypatch: pytest.MonkeyPatch) -> None:
        captured: list[_Call] = []
        monkeypatch.setattr(
            slack_http.httpx,
            "post",
            _capturing_post(captured, {"ok": False, "error": "channel_not_found"}),
        )

        assert _backend().post_routed(channel="C_GONE", text="x") == {"ok": False, "error": "channel_not_found"}

    def test_no_credentials_returns_empty_without_calling(self, monkeypatch: pytest.MonkeyPatch) -> None:
        called: list[object] = []

        def fail_post(url: str, **kwargs: object) -> httpx.Response:
            called.append(url)
            return httpx.Response(200, json={"ok": True}, request=httpx.Request("POST", url))

        monkeypatch.setattr(slack_http.httpx, "post", fail_post)

        assert SlackBotBackend().post_routed(channel="C_TEAM", text="x") == {}
        assert called == []


class TestReactRouted:
    def test_react_self_dm_under_bot_token(self, monkeypatch: pytest.MonkeyPatch) -> None:
        captured: list[_Call] = []
        monkeypatch.setattr(slack_http.httpx, "post", _capturing_post(captured, {"ok": True}))

        _backend().react_routed(channel=_SELF_DM, ts="1.2", emoji="eyes")

        assert captured[0].url.endswith("/reactions.add")
        assert captured[0].authorization == "Bearer xoxb-bot"

    def test_react_colleague_under_user_token(self, monkeypatch: pytest.MonkeyPatch) -> None:
        captured: list[_Call] = []
        monkeypatch.setattr(slack_http.httpx, "post", _capturing_post(captured, {"ok": True}))

        _backend().react_routed(channel="D_COLLEAGUE", ts="1.2", emoji="eyes")

        assert captured[0].authorization == "Bearer xoxp-user"

    def test_react_channel_under_user_token(self, monkeypatch: pytest.MonkeyPatch) -> None:
        captured: list[_Call] = []
        monkeypatch.setattr(slack_http.httpx, "post", _capturing_post(captured, {"ok": True}))

        _backend().react_routed(channel="C_TEAM", ts="1.2", emoji="eyes")

        assert captured[0].authorization == "Bearer xoxp-user"
        assert captured[0].json == {"channel": "C_TEAM", "timestamp": "1.2", "name": "eyes"}

    def test_returns_error_body_on_missing_scope(self, monkeypatch: pytest.MonkeyPatch) -> None:
        captured: list[_Call] = []
        monkeypatch.setattr(
            slack_http.httpx,
            "post",
            _capturing_post(captured, {"ok": False, "error": "missing_scope", "needed": "reactions:write"}),
        )

        result = _backend().react_routed(channel="C_TEAM", ts="1.2", emoji="eyes")

        assert result == {"ok": False, "error": "missing_scope", "needed": "reactions:write"}

    def test_no_credentials_returns_empty_without_calling(self, monkeypatch: pytest.MonkeyPatch) -> None:
        called: list[object] = []

        def fail_post(url: str, **kwargs: object) -> httpx.Response:
            called.append(url)
            return httpx.Response(200, json={"ok": True}, request=httpx.Request("POST", url))

        monkeypatch.setattr(slack_http.httpx, "post", fail_post)

        assert SlackBotBackend().react_routed(channel="C_TEAM", ts="1.2", emoji="eyes") == {}
        assert called == []
