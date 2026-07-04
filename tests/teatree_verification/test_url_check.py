"""The deterministic URL existence check (PR-15, M5).

``check_url`` distinguishes OK / UNRESOLVED / NETWORK_ERROR, is HEAD-first with a
ranged-GET fallback, and never touches the network in tests (the transport is an
injected ``opener``). Each assertion fails if the status mapping regresses.
"""

import urllib.error
import urllib.request
from typing import Self

from teatree.verification.url_check import UrlCheckStatus, check_url


class _FakeResponse:
    def __init__(self, status: int) -> None:
        self.status = status

    def __enter__(self) -> Self:
        return self

    def __exit__(self, *_: object) -> None:
        return None


def _opener(method_status: dict[str, object]) -> object:
    """Build an opener that maps request method -> a status int or an exception."""

    def opener(request: urllib.request.Request, timeout: float) -> _FakeResponse:
        outcome = method_status[request.get_method()]
        if isinstance(outcome, Exception):
            raise outcome
        return _FakeResponse(int(outcome))

    return opener


class TestCheckUrl:
    def test_head_2xx_is_ok(self) -> None:
        result = check_url("https://example.com/a", opener=_opener({"HEAD": 200}))
        assert result.status is UrlCheckStatus.OK
        assert result.http_status == 200

    def test_3xx_is_ok(self) -> None:
        result = check_url("https://example.com/a", opener=_opener({"HEAD": 302}))
        assert result.status is UrlCheckStatus.OK

    def test_404_on_both_methods_is_unresolved(self) -> None:
        err = urllib.error.HTTPError("https://example.com/x", 404, "Not Found", {}, None)
        result = check_url("https://example.com/x", opener=_opener({"HEAD": err, "GET": err}))
        assert result.status is UrlCheckStatus.UNRESOLVED
        assert result.http_status == 404

    def test_head_405_falls_back_to_get_ok(self) -> None:
        err = urllib.error.HTTPError("https://example.com/y", 405, "Method Not Allowed", {}, None)
        result = check_url("https://example.com/y", opener=_opener({"HEAD": err, "GET": 206}))
        assert result.status is UrlCheckStatus.OK
        assert result.http_status == 206

    def test_network_failure_is_network_error(self) -> None:
        err = urllib.error.URLError("name resolution failed")
        result = check_url("https://nope.invalid/z", opener=_opener({"HEAD": err, "GET": err}))
        assert result.status is UrlCheckStatus.NETWORK_ERROR
        assert not result.ok

    def test_timeout_is_network_error(self) -> None:
        timed_out = {"HEAD": TimeoutError("timed out"), "GET": TimeoutError("timed out")}
        result = check_url("https://slow.example/z", opener=_opener(timed_out))
        assert result.status is UrlCheckStatus.NETWORK_ERROR

    def test_non_http_scheme_is_network_error(self) -> None:
        # An unverifiable scheme is not "unresolved" — teatree cannot probe it, so
        # the caller must NOT drop on it. Never touches the opener.
        result = check_url("ftp://example.com/file", opener=_opener({}))
        assert result.status is UrlCheckStatus.NETWORK_ERROR

    def test_blank_url_is_network_error(self) -> None:
        assert check_url("   ", opener=_opener({})).status is UrlCheckStatus.NETWORK_ERROR
