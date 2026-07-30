"""Tests for the shared bounded-retry transport and the backends now built on it.

The retry contract is exercised end-to-end through :class:`SimpleRetryTransport`
(a transient connect/response failure is retried with backoff; a non-idempotent
write is NOT replayed on a response-phase failure; a ``Retry-After`` is
honoured), and the Sentry / Notion / Figma clients are shown to actually gain
that retry — the failure their docstrings say broke a loop tick.
"""

from collections.abc import Iterator
from unittest.mock import MagicMock, patch

import httpx
import pytest

from teatree.backends.figma import FigmaClient
from teatree.backends.gitlab.http_client import GitLabHTTPClient
from teatree.backends.http_retry import (
    DEFAULT_BACKOFF_BASE_SECONDS,
    DEFAULT_MAX_RETRIES,
    DEFAULT_TIMEOUT_SECONDS,
    BoundedRetryTransport,
    RetryClass,
    SimpleRetryTransport,
    env_float,
    env_int,
)
from teatree.backends.notion import NotionClient
from teatree.backends.sentry import SentryClient
from teatree.backends.slack.http import SlackHttpClient


def _ok(body: object = None) -> httpx.Response:
    return httpx.Response(200, json=body if body is not None else {"ok": True}, request=_req())


def _status(code: int) -> httpx.Response:
    return httpx.Response(code, request=_req())


def _req() -> httpx.Request:
    return httpx.Request("GET", "https://example.test/x")


def _raise_or_return(calls: Iterator[httpx.Response | BaseException]) -> httpx.Response:
    item = next(calls)
    if isinstance(item, BaseException):
        raise item
    return item


def _transport(sleeps: list[float]) -> SimpleRetryTransport:
    return SimpleRetryTransport(env_prefix="T3_TEST_HTTP", max_retries=3, backoff_base=0.5, sleep=sleeps.append)


class TestEnvHelpers:
    @pytest.mark.parametrize(("raw", "expected"), [("", 10.0), ("bad", 10.0), ("-1", 10.0), ("0", 10.0), ("2.5", 2.5)])
    def test_env_float_falls_back_unless_positive(
        self, raw: str, expected: float, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("T3_TEST_HTTP_X", raw)
        assert env_float("T3_TEST_HTTP_X", 10.0) == pytest.approx(expected)

    @pytest.mark.parametrize(("raw", "expected"), [("", 3), ("bad", 3), ("-1", 3), ("0", 0), ("5", 5)])
    def test_env_int_falls_back_unless_non_negative(
        self, raw: str, expected: int, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("T3_TEST_HTTP_N", raw)
        assert env_int("T3_TEST_HTTP_N", 3) == expected

    def test_defaults_resolve_from_prefix_when_unset(self, monkeypatch: pytest.MonkeyPatch) -> None:
        for suffix in ("MAX_RETRIES", "BACKOFF"):
            monkeypatch.delenv(f"T3_TEST_HTTP_{suffix}", raising=False)
        transport = SimpleRetryTransport(env_prefix="T3_TEST_HTTP")
        assert transport._max_retries == DEFAULT_MAX_RETRIES
        assert transport._backoff_base == pytest.approx(DEFAULT_BACKOFF_BASE_SECONDS)
        assert pytest.approx(10.0) == DEFAULT_TIMEOUT_SECONDS


class TestConcreteTransportsShareTheBase:
    """The refactor invariant: both reliability-critical clients ARE the shared machine."""

    def test_gitlab_and_slack_build_on_bounded_retry_transport(self) -> None:
        assert issubclass(GitLabHTTPClient, BoundedRetryTransport)
        assert issubclass(SlackHttpClient, BoundedRetryTransport)

    def test_raise_for_status_seam_differs(self) -> None:
        # Slack raises on a persistent 5xx; GitLab hands the raw response back.
        assert SlackHttpClient._RAISE_FOR_STATUS_ON_RETURN is True
        assert GitLabHTTPClient._RAISE_FOR_STATUS_ON_RETURN is False

    def test_retry_class_gating(self) -> None:
        # CONNECT is always retryable; RESPONSE only for an idempotent call.
        may = BoundedRetryTransport._may_retry
        assert may(RetryClass.CONNECT, idempotent=False) is True
        assert may(RetryClass.CONNECT, idempotent=True) is True
        assert may(RetryClass.RESPONSE, idempotent=False) is False
        assert may(RetryClass.RESPONSE, idempotent=True) is True


class TestSimpleRetryTransport:
    def test_server_error_then_success_retries_idempotent(self) -> None:
        sleeps: list[float] = []
        calls = iter([_status(503), _ok({"n": 1})])
        body = _transport(sleeps).run(lambda: _raise_or_return(calls), idempotent=True)
        assert body.json() == {"n": 1}
        assert sleeps == [0.5]

    def test_connect_error_retried_even_for_non_idempotent(self) -> None:
        sleeps: list[float] = []
        calls = iter([httpx.ConnectError("down"), _ok({"n": 2})])
        body = _transport(sleeps).run(lambda: _raise_or_return(calls), idempotent=False)
        assert body.json() == {"n": 2}
        assert sleeps == [0.5]

    def test_non_idempotent_response_failure_is_not_replayed(self) -> None:
        sleeps: list[float] = []
        attempts: list[int] = []

        def attempt() -> httpx.Response:
            attempts.append(1)
            return _status(503)

        # SimpleRetryTransport does not raise_for_status; the 503 surfaces so the
        # caller's own raise_for_status decides — but it must NOT have been retried.
        response = _transport(sleeps).run(attempt, idempotent=False)
        assert response.status_code == 503
        assert attempts == [1]
        assert sleeps == []

    def test_retry_after_header_is_honoured(self) -> None:
        sleeps: list[float] = []
        limited = httpx.Response(429, headers={"Retry-After": "7"}, request=_req())
        calls = iter([limited, _ok()])
        _transport(sleeps).run(lambda: _raise_or_return(calls), idempotent=True)
        assert sleeps == [7.0]

    def test_exhaustion_raises_last_connect_error(self) -> None:
        sleeps: list[float] = []
        down = httpx.ConnectError("always")

        def attempt() -> httpx.Response:
            raise down

        with pytest.raises(httpx.ConnectError):
            _transport(sleeps).run(attempt, idempotent=True)
        assert sleeps == [0.5, 1.0, 2.0]


def _fake_client(responses: list[httpx.Response | BaseException]) -> MagicMock:
    calls = iter(responses)
    client = MagicMock()
    client.__enter__ = MagicMock(return_value=client)
    client.__exit__ = MagicMock(return_value=False)
    for verb in ("get", "post", "patch"):
        getattr(client, verb).side_effect = lambda *_a, _c=calls, **_k: _raise_or_return(_c)
    return client


class TestBackendsNowRetry:
    def test_sentry_retries_transient_5xx(self) -> None:
        client = SentryClient(token="t", org="o")
        sleeps: list[float] = []
        client._transport._sleep = sleeps.append
        fake = _fake_client([_status(503), _ok([{"id": "1"}])])

        with patch.object(client, "_client", return_value=fake):
            issues = client.get_top_issues(project="p")

        assert issues == [{"id": "1"}]
        assert fake.get.call_count == 2
        assert sleeps == [0.5]

    def test_figma_retries_transient_5xx(self) -> None:
        client = FigmaClient(token="t")
        sleeps: list[float] = []
        client._transport._sleep = sleeps.append
        fake = _fake_client([_status(503), _ok({"document": {}, "components": {}})])

        with patch.object(client, "_client", return_value=fake):
            file_data = client.get_file("fk")

        assert file_data == {"document": {}, "components": {}}
        assert fake.get.call_count == 2
        assert sleeps == [0.5]

    def test_notion_query_retries_transient_5xx(self) -> None:
        client = NotionClient(token="t")
        sleeps: list[float] = []
        client._transport._sleep = sleeps.append
        fake = _fake_client([_status(503), _ok({"results": [{"id": "r1"}], "has_more": False})])

        with patch.object(client, "_client", return_value=fake):
            rows = client.query_database("db")

        assert [r["id"] for r in rows] == ["r1"]
        assert fake.post.call_count == 2
        assert sleeps == [0.5]

    def test_notion_update_is_non_idempotent_no_response_replay(self) -> None:
        client = NotionClient(token="t")
        sleeps: list[float] = []
        client._transport._sleep = sleeps.append
        # A 503 on the PATCH must NOT be replayed (a write may have landed); the
        # caller's raise_for_status then surfaces it.
        fake = _fake_client([_status(503)])

        with patch.object(client, "_client", return_value=fake), pytest.raises(httpx.HTTPStatusError):
            client.update_page_status("pg", property_name="Status", value="Done")

        assert fake.patch.call_count == 1
        assert sleeps == []
