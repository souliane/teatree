"""Tests for ``SlackHttpClient`` — bounded retry on transient Slack failures.

Only the httpx boundary and the clock are mocked. The retry contract:
a transient connect/response failure is retried with backoff, a
non-idempotent post is NOT replayed on a response-phase failure (it may
have already posted), and a ``Retry-After`` is honoured.
"""

from collections.abc import Iterator

import httpx
import pytest

import teatree.core.intake.scope_cache as scope_cache_module
from teatree.backends.slack import http as slack_http
from teatree.backends.slack.http import SlackHttpClient


def _ok(body: dict[str, object] | None = None) -> httpx.Response:
    return httpx.Response(200, json=body or {"ok": True}, request=httpx.Request("POST", "https://slack.com/api/x"))


def _client(sleeps: list[float]) -> SlackHttpClient:
    return SlackHttpClient(timeout=1.0, max_retries=3, backoff_base=0.5, sleep=sleeps.append)


class TestRetryOnTransientThenSuccess:
    def test_read_timeout_then_success_on_get(self, monkeypatch: pytest.MonkeyPatch) -> None:
        sleeps: list[float] = []
        calls = iter([httpx.ReadTimeout("slow"), _ok({"ok": True, "n": 1})])
        monkeypatch.setattr(slack_http.httpx, "get", lambda *a, **k: _raise_or_return(calls))

        body = _client(sleeps).get("conversations.info", token="xoxb-x", params={"channel": "C1"})

        assert body == {"ok": True, "n": 1}
        assert sleeps == [0.5]

    def test_connect_error_then_success_on_post(self, monkeypatch: pytest.MonkeyPatch) -> None:
        sleeps: list[float] = []
        calls = iter([httpx.ConnectError("down"), _ok({"ok": True, "ts": "1.2"})])
        monkeypatch.setattr(slack_http.httpx, "post", lambda *a, **k: _raise_or_return(calls))

        body = _client(sleeps).post("chat.postMessage", token="xoxb-x", json={"channel": "C1"}, idempotent=False)

        assert body == {"ok": True, "ts": "1.2"}
        assert sleeps == [0.5]

    def test_server_error_then_success_retries(self, monkeypatch: pytest.MonkeyPatch) -> None:
        sleeps: list[float] = []
        responses = iter(
            [
                httpx.Response(503, request=httpx.Request("POST", "https://slack.com/api/x")),
                _ok({"ok": True}),
            ]
        )
        monkeypatch.setattr(slack_http.httpx, "post", lambda *a, **k: next(responses))

        body = _client(sleeps).post("reactions.add", token="xoxb-x", json={}, idempotent=True)

        assert body == {"ok": True}
        assert sleeps == [0.5]

    def test_slack_ratelimited_honours_retry_after(self, monkeypatch: pytest.MonkeyPatch) -> None:
        sleeps: list[float] = []
        limited = httpx.Response(
            200,
            json={"ok": False, "error": "ratelimited"},
            headers={"Retry-After": "7"},
            request=httpx.Request("POST", "https://slack.com/api/x"),
        )
        responses = iter([limited, _ok({"ok": True})])
        monkeypatch.setattr(slack_http.httpx, "get", lambda *a, **k: next(responses))

        body = _client(sleeps).get("conversations.history", token="xoxb-x", params={"channel": "C1"})

        assert body == {"ok": True}
        assert sleeps == [7.0]


class TestNonIdempotentPostNotReplayedOnResponseFailure:
    def test_read_timeout_on_non_idempotent_post_does_not_retry(self, monkeypatch: pytest.MonkeyPatch) -> None:
        sleeps: list[float] = []
        attempts: list[int] = []

        timeout = httpx.ReadTimeout("response lost")

        def fake_post(*_args: object, **_kwargs: object) -> httpx.Response:
            attempts.append(1)
            raise timeout

        monkeypatch.setattr(slack_http.httpx, "post", fake_post)

        with pytest.raises(httpx.ReadTimeout):
            _client(sleeps).post("chat.postMessage", token="xoxb-x", json={"channel": "C1"}, idempotent=False)

        assert attempts == [1]
        assert sleeps == []

    def test_idempotent_post_does_retry_on_read_timeout(self, monkeypatch: pytest.MonkeyPatch) -> None:
        sleeps: list[float] = []
        calls = iter([httpx.ReadTimeout("slow"), _ok({"ok": True})])
        monkeypatch.setattr(slack_http.httpx, "post", lambda *a, **k: _raise_or_return(calls))

        body = _client(sleeps).post("reactions.add", token="xoxb-x", json={}, idempotent=True)

        assert body == {"ok": True}
        assert sleeps == [0.5]


class TestExhaustionRaisesLastError:
    def test_persistent_connect_error_raises_after_retries(self, monkeypatch: pytest.MonkeyPatch) -> None:
        sleeps: list[float] = []
        down = httpx.ConnectError("always down")

        def always_fail(*_args: object, **_kwargs: object) -> httpx.Response:
            raise down

        monkeypatch.setattr(slack_http.httpx, "post", always_fail)

        with pytest.raises(httpx.ConnectError):
            _client(sleeps).post("reactions.add", token="xoxb-x", json={}, idempotent=True)

        assert sleeps == [0.5, 1.0, 2.0]


class TestAuthTestHeaderPassThrough:
    def test_post_with_header_returns_body_and_named_header(self, monkeypatch: pytest.MonkeyPatch) -> None:
        response = httpx.Response(
            200,
            json={"ok": True, "user_id": "U1"},
            headers={"X-OAuth-Scopes": "chat:write,reactions:write"},
            request=httpx.Request("POST", "https://slack.com/api/auth.test"),
        )
        monkeypatch.setattr(slack_http.httpx, "post", lambda *a, **k: response)

        body, header = SlackHttpClient(sleep=lambda _s: None).post_with_header(
            "auth.test", token="xoxb-x", json={}, header="X-OAuth-Scopes"
        )

        assert body == {"ok": True, "user_id": "U1"}
        assert header == "chat:write,reactions:write"


class TestResponseEdgeCases:
    def test_429_status_retries_for_idempotent(self, monkeypatch: pytest.MonkeyPatch) -> None:
        sleeps: list[float] = []
        too_many = httpx.Response(429, request=httpx.Request("GET", "https://slack.com/api/x"))
        responses = iter([too_many, _ok({"ok": True})])
        monkeypatch.setattr(slack_http.httpx, "get", lambda *a, **k: next(responses))

        body = _client(sleeps).get("conversations.info", token="xoxb-x", params={})

        assert body == {"ok": True}
        assert sleeps == [0.5]

    def test_non_idempotent_post_surfaces_5xx_without_retry(self, monkeypatch: pytest.MonkeyPatch) -> None:
        sleeps: list[float] = []
        attempts: list[int] = []

        def fake_post(*_args: object, **_kwargs: object) -> httpx.Response:
            attempts.append(1)
            return httpx.Response(503, request=httpx.Request("POST", "https://slack.com/api/x"))

        monkeypatch.setattr(slack_http.httpx, "post", fake_post)

        with pytest.raises(httpx.HTTPStatusError):
            _client(sleeps).post("chat.postMessage", token="xoxb-x", json={}, idempotent=False)

        assert attempts == [1]
        assert sleeps == []

    def test_ratelimited_without_retry_after_uses_backoff(self, monkeypatch: pytest.MonkeyPatch) -> None:
        sleeps: list[float] = []
        limited = httpx.Response(
            200,
            json={"ok": False, "error": "ratelimited"},
            request=httpx.Request("GET", "https://slack.com/api/x"),
        )
        responses = iter([limited, _ok({"ok": True})])
        monkeypatch.setattr(slack_http.httpx, "get", lambda *a, **k: next(responses))

        body = _client(sleeps).get("conversations.history", token="xoxb-x", params={})

        assert body == {"ok": True}
        assert sleeps == [0.5]

    def test_invalid_retry_after_header_falls_back_to_backoff(self, monkeypatch: pytest.MonkeyPatch) -> None:
        sleeps: list[float] = []
        limited = httpx.Response(
            200,
            json={"ok": False, "error": "ratelimited"},
            headers={"Retry-After": "soon"},
            request=httpx.Request("GET", "https://slack.com/api/x"),
        )
        responses = iter([limited, _ok({"ok": True})])
        monkeypatch.setattr(slack_http.httpx, "get", lambda *a, **k: next(responses))

        body = _client(sleeps).get("conversations.history", token="xoxb-x", params={})

        assert body == {"ok": True}
        assert sleeps == [0.5]

    def test_plain_ok_false_is_not_retried(self, monkeypatch: pytest.MonkeyPatch) -> None:
        sleeps: list[float] = []
        attempts: list[int] = []

        def fake_get(*_args: object, **_kwargs: object) -> httpx.Response:
            attempts.append(1)
            return _ok({"ok": False, "error": "channel_not_found"})

        monkeypatch.setattr(slack_http.httpx, "get", fake_get)

        body = _client(sleeps).get("conversations.info", token="xoxb-x", params={})

        assert body == {"ok": False, "error": "channel_not_found"}
        assert attempts == [1]
        assert sleeps == []


class TestEnvConfiguration:
    def test_env_overrides_timeout_and_retries_and_backoff(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("T3_SLACK_HTTP_TIMEOUT", "42")
        monkeypatch.setenv("T3_SLACK_HTTP_MAX_RETRIES", "0")
        monkeypatch.setenv("T3_SLACK_HTTP_BACKOFF", "2.5")
        client = SlackHttpClient()
        assert client._timeout == pytest.approx(42.0)
        assert client._max_retries == 0
        assert client._backoff_base == pytest.approx(2.5)

    @pytest.mark.parametrize("raw", ["", "not-a-number", "-1", "0"])
    def test_invalid_or_nonpositive_timeout_falls_back_to_default(
        self, raw: str, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("T3_SLACK_HTTP_TIMEOUT", raw)
        assert SlackHttpClient()._timeout == slack_http.DEFAULT_TIMEOUT_SECONDS

    @pytest.mark.parametrize("raw", ["", "not-a-number", "-1"])
    def test_invalid_or_negative_retries_falls_back_to_default(self, raw: str, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("T3_SLACK_HTTP_MAX_RETRIES", raw)
        assert SlackHttpClient()._max_retries == slack_http.DEFAULT_MAX_RETRIES

    def test_zero_retries_is_honoured(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("T3_SLACK_HTTP_MAX_RETRIES", "0")
        assert SlackHttpClient()._max_retries == 0


def _missing_scope(scope: str) -> httpx.Response:
    return httpx.Response(
        200,
        json={"ok": False, "error": "missing_scope", "needed": scope, "provided": "channels:read,chat:write"},
        request=httpx.Request("POST", "https://slack.com/api/x"),
    )


class TestScopeCacheWiring:
    """The token-scope cache short-circuits a known-missing scope pre-HTTP (PR-19 item 6)."""

    def test_missing_scope_caches_and_second_call_skips_http(self, monkeypatch: pytest.MonkeyPatch) -> None:
        banners: list[str] = []
        monkeypatch.setattr(
            scope_cache_module,
            "_CACHE",
            scope_cache_module.ScopeCache(
                notifier=lambda text, *, kind, idempotency_key: banners.append(idempotency_key) is None,
            ),
        )
        posts: list[int] = []

        def fake_post(*_a: object, **_k: object) -> httpx.Response:
            posts.append(1)
            return _missing_scope("reactions:write")

        monkeypatch.setattr(slack_http.httpx, "post", fake_post)
        client = _client([])

        first = client.post("reactions.add", token="xoxb-x", json={}, idempotent=True)
        second = client.post("reactions.add", token="xoxb-x", json={}, idempotent=True)

        # Live failure echoes Slack's verbatim body (``provided`` intact); the
        # pre-HTTP short-circuit has no response to echo, so it reconstructs the
        # minimal body every caller tolerates.
        assert first == {
            "ok": False,
            "error": "missing_scope",
            "needed": "reactions:write",
            "provided": "channels:read,chat:write",
        }
        assert second == {"ok": False, "error": "missing_scope", "needed": "reactions:write"}
        assert len(posts) == 1  # second call short-circuited pre-HTTP
        assert banners == ["scope_missing:" + slack_http.token_scope_id("xoxb-x") + ":reactions:write"]

    def test_unmapped_method_is_not_guarded(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(
            scope_cache_module,
            "_CACHE",
            scope_cache_module.ScopeCache(notifier=lambda *a, **k: True),
        )
        posts: list[int] = []

        def fake_post(*_a: object, **_k: object) -> httpx.Response:
            posts.append(1)
            return _missing_scope("chat:write")

        monkeypatch.setattr(slack_http.httpx, "post", fake_post)
        client = _client([])
        # ``chat.update`` is not in SLACK_METHOD_SCOPES → every call hits HTTP.
        client.post("chat.update", token="xoxb-x", json={}, idempotent=True)
        client.post("chat.update", token="xoxb-x", json={}, idempotent=True)
        assert len(posts) == 2


def _raise_or_return(calls: Iterator[httpx.Response | BaseException]) -> httpx.Response:
    item = next(calls)
    if isinstance(item, BaseException):
        raise item
    return item
