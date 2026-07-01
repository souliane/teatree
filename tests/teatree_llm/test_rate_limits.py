r"""The foundation rate-limit reader (``teatree.llm.rate_limits``).

DB-free, network-free: every test drives a fake :class:`~teatree.llm.rate_limits.Transport`
returning a canned ``(status, headers)`` pair, so the parsing is verified against the
exact wire headers without a real ``/v1/messages`` call. A 200 and a 429 are asserted
to parse ALIKE (both carry the rate-limit headers); only a non-{200,429} status or a
transport error is a failure. The token is asserted to sign the request yet never
appear on the returned snapshot.
"""

import datetime as dt
from collections.abc import Mapping

import httpx
import pytest

from teatree.llm.rate_limits import ProbeResponse, RateLimitProbeError, RateLimitSnapshot, read_rate_limits

_ORG = "anthropic-organization-id"
_RETRY_AFTER = "retry-after"
_5H_STATUS = "anthropic-ratelimit-unified-5h-status"
_5H_UTIL = "anthropic-ratelimit-unified-5h-utilization"
_5H_RESET = "anthropic-ratelimit-unified-5h-reset"
_7D_STATUS = "anthropic-ratelimit-unified-7d-status"
_7D_UTIL = "anthropic-ratelimit-unified-7d-utilization"
_7D_RESET = "anthropic-ratelimit-unified-7d-reset"

_FULL_HEADERS = {
    _ORG: "org-abc123",
    _RETRY_AFTER: "42",
    _5H_STATUS: "allowed",
    _5H_UTIL: "0.30",
    _5H_RESET: "2026-07-01T18:00:00Z",
    _7D_STATUS: "allowed_warning",
    _7D_UTIL: "0.80",
    _7D_RESET: "2026-07-08T00:00:00+00:00",
}


class _RecordingTransport:
    """A fake transport that records what it was called with and returns a canned response."""

    def __init__(self, response: ProbeResponse) -> None:
        self._response = response
        self.headers: Mapping[str, str] = {}
        self.body: Mapping[str, object] = {}

    def __call__(self, headers: Mapping[str, str], body: Mapping[str, object]) -> ProbeResponse:
        self.headers = headers
        self.body = body
        return self._response


def _read(status: int, headers: Mapping[str, str], *, is_oauth: bool = False) -> RateLimitSnapshot:
    transport = _RecordingTransport(ProbeResponse(status_code=status, headers=headers))
    return read_rate_limits("secret-token", is_oauth=is_oauth, transport=transport)


class TestHeaderParsing:
    def test_full_headers_parse_into_every_field(self) -> None:
        snap = _read(200, _FULL_HEADERS)
        assert snap.organization_id == "org-abc123"
        assert snap.unified_5h_status == "allowed"
        assert snap.unified_5h_utilization == pytest.approx(0.30)
        assert snap.unified_7d_status == "allowed_warning"
        assert snap.unified_7d_utilization == pytest.approx(0.80)
        assert snap.retry_after == 42

    def test_reset_headers_parse_to_tz_aware_utc_datetimes(self) -> None:
        snap = _read(200, _FULL_HEADERS)
        assert snap.unified_5h_reset == dt.datetime(2026, 7, 1, 18, 0, tzinfo=dt.UTC)
        assert snap.unified_5h_reset is not None
        assert snap.unified_5h_reset.tzinfo is not None
        assert snap.unified_7d_reset == dt.datetime(2026, 7, 8, 0, 0, tzinfo=dt.UTC)

    def test_429_carries_the_same_headers_as_200(self) -> None:
        # A 429 STILL returns the rate-limit headers, so it parses identically to a 200.
        assert _read(429, _FULL_HEADERS) == _read(200, _FULL_HEADERS)

    def test_missing_headers_default_to_empty_and_none(self) -> None:
        snap = _read(200, {})
        assert snap.organization_id == ""
        assert snap.unified_5h_status == ""
        assert snap.unified_5h_utilization == pytest.approx(0.0)
        assert snap.unified_5h_reset is None
        assert snap.retry_after is None

    def test_case_insensitive_header_lookup(self) -> None:
        snap = _read(200, {_5H_UTIL.upper(): "0.5", "Anthropic-Organization-Id": "org-x"})
        assert snap.unified_5h_utilization == pytest.approx(0.5)
        assert snap.organization_id == "org-x"

    def test_unparseable_numeric_and_reset_headers_degrade_not_crash(self) -> None:
        snap = _read(200, {_5H_UTIL: "n/a", _RETRY_AFTER: "soon", _5H_RESET: "not-a-date"})
        assert snap.unified_5h_utilization == pytest.approx(0.0)
        assert snap.retry_after is None
        assert snap.unified_5h_reset is None


class TestFailureModes:
    @pytest.mark.parametrize("status", [400, 401, 403, 500, 529])
    def test_non_200_or_429_status_raises(self, status: int) -> None:
        with pytest.raises(RateLimitProbeError):
            _read(status, _FULL_HEADERS)

    def test_transport_network_error_raises_probe_error(self) -> None:
        def boom(_headers: Mapping[str, str], _body: Mapping[str, object]) -> ProbeResponse:
            msg = "no route to host"
            raise httpx.ConnectError(msg)

        with pytest.raises(RateLimitProbeError):
            read_rate_limits("t", is_oauth=False, transport=boom)


class TestRequestSigningAndTokenSafety:
    def test_token_signs_the_request_but_is_absent_from_the_snapshot(self) -> None:
        transport = _RecordingTransport(ProbeResponse(status_code=200, headers=_FULL_HEADERS))
        snapshot = read_rate_limits("sk-super-secret", is_oauth=False, transport=transport)
        assert transport.headers["authorization"] == "Bearer sk-super-secret"
        assert "sk-super-secret" not in repr(snapshot), "the token must never be carried on the snapshot"

    def test_oauth_probe_sends_the_beta_header_and_api_key_probe_does_not(self) -> None:
        oauth = _RecordingTransport(ProbeResponse(status_code=200, headers={}))
        read_rate_limits("t", is_oauth=True, transport=oauth)
        assert oauth.headers["anthropic-beta"] == "oauth-2025-04-20"

        api_key = _RecordingTransport(ProbeResponse(status_code=200, headers={}))
        read_rate_limits("t", is_oauth=False, transport=api_key)
        assert "anthropic-beta" not in api_key.headers

    def test_probe_body_is_a_one_token_haiku_ping(self) -> None:
        transport = _RecordingTransport(ProbeResponse(status_code=200, headers={}))
        read_rate_limits("t", is_oauth=False, transport=transport)
        assert transport.body["model"] == "claude-haiku-4-5"
        assert transport.body["max_tokens"] == 1
