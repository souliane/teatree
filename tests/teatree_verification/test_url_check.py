"""The deterministic URL existence check (PR-15, M5).

``check_url`` distinguishes OK / UNRESOLVED / NETWORK_ERROR, is HEAD-first with a
ranged-GET fallback, and never touches the network in tests (the transport is an
injected ``opener``; the host resolution is an injected ``resolver``). Each
assertion fails if the status mapping — or the SSRF host filter — regresses.
"""

import socket
import urllib.error
import urllib.request
from typing import Self

from teatree.verification.url_check import HostResolver, UrlCheckStatus, check_url


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


def _public_resolver(_host: str) -> list[str]:
    """A host resolver that always yields a public address (no network in tests)."""
    return ["93.184.216.34"]


def _resolver_to(address: str) -> HostResolver:
    def resolve(_host: str) -> list[str]:
        return [address]

    return resolve


class TestCheckUrl:
    def test_head_2xx_is_ok(self) -> None:
        result = check_url("https://example.com/a", opener=_opener({"HEAD": 200}), resolver=_public_resolver)
        assert result.status is UrlCheckStatus.OK
        assert result.http_status == 200

    def test_3xx_is_ok(self) -> None:
        result = check_url("https://example.com/a", opener=_opener({"HEAD": 302}), resolver=_public_resolver)
        assert result.status is UrlCheckStatus.OK

    def test_404_on_both_methods_is_unresolved(self) -> None:
        err = urllib.error.HTTPError("https://example.com/x", 404, "Not Found", {}, None)
        result = check_url(
            "https://example.com/x", opener=_opener({"HEAD": err, "GET": err}), resolver=_public_resolver
        )
        assert result.status is UrlCheckStatus.UNRESOLVED
        assert result.http_status == 404

    def test_head_405_falls_back_to_get_ok(self) -> None:
        err = urllib.error.HTTPError("https://example.com/y", 405, "Method Not Allowed", {}, None)
        result = check_url(
            "https://example.com/y", opener=_opener({"HEAD": err, "GET": 206}), resolver=_public_resolver
        )
        assert result.status is UrlCheckStatus.OK
        assert result.http_status == 206

    def test_network_failure_is_network_error(self) -> None:
        err = urllib.error.URLError("name resolution failed")
        result = check_url(
            "https://nope.invalid/z", opener=_opener({"HEAD": err, "GET": err}), resolver=_public_resolver
        )
        assert result.status is UrlCheckStatus.NETWORK_ERROR
        assert not result.ok

    def test_timeout_is_network_error(self) -> None:
        timed_out = {"HEAD": TimeoutError("timed out"), "GET": TimeoutError("timed out")}
        result = check_url("https://slow.example/z", opener=_opener(timed_out), resolver=_public_resolver)
        assert result.status is UrlCheckStatus.NETWORK_ERROR

    def test_non_http_scheme_is_network_error(self) -> None:
        # An unverifiable scheme is not "unresolved" — teatree cannot probe it, so
        # the caller must NOT drop on it. Never touches the opener.
        result = check_url("ftp://example.com/file", opener=_opener({}), resolver=_public_resolver)
        assert result.status is UrlCheckStatus.NETWORK_ERROR

    def test_blank_url_is_network_error(self) -> None:
        assert check_url("   ", opener=_opener({}), resolver=_public_resolver).status is UrlCheckStatus.NETWORK_ERROR


class TestSsrfGuard:
    """The host SSRF filter refuses non-public addresses before any probe."""

    def _explode_opener(self, request: urllib.request.Request, timeout: float) -> _FakeResponse:
        msg = "the opener must never be reached for a refused host"
        raise AssertionError(msg)

    def test_loopback_is_refused_without_probing(self) -> None:
        result = check_url(
            "http://localhost/latest/meta-data",
            opener=self._explode_opener,
            resolver=_resolver_to("127.0.0.1"),
        )
        assert result.status is UrlCheckStatus.UNRESOLVED
        assert "non-public" in result.detail

    def test_cloud_metadata_ip_is_refused(self) -> None:
        result = check_url(
            "http://169.254.169.254/latest/meta-data/iam/",
            opener=self._explode_opener,
            resolver=_resolver_to("169.254.169.254"),
        )
        assert result.status is UrlCheckStatus.UNRESOLVED

    def test_private_rfc1918_is_refused(self) -> None:
        result = check_url(
            "http://internal.example/admin",
            opener=self._explode_opener,
            resolver=_resolver_to("10.0.0.5"),
        )
        assert result.status is UrlCheckStatus.UNRESOLVED

    def test_any_private_resolved_address_is_refused(self) -> None:
        # A host that resolves to BOTH a public and a private address is refused —
        # the private hop is the SSRF vector even when a public one exists.
        def mixed(_host: str) -> list[str]:
            return ["93.184.216.34", "127.0.0.1"]

        result = check_url("https://rebind.example/x", opener=self._explode_opener, resolver=mixed)
        assert result.status is UrlCheckStatus.UNRESOLVED

    def test_dns_resolution_failure_is_network_error(self) -> None:
        def failing_resolver(_host: str) -> list[str]:
            msg = "name or service not known"
            raise socket.gaierror(msg)

        result = check_url("https://nope.invalid/z", opener=self._explode_opener, resolver=failing_resolver)
        assert result.status is UrlCheckStatus.NETWORK_ERROR

    def test_public_address_is_probed(self) -> None:
        result = check_url(
            "https://example.com/a", opener=_opener({"HEAD": 200}), resolver=_resolver_to("93.184.216.34")
        )
        assert result.status is UrlCheckStatus.OK
