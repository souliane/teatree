r"""The foundation rate-limit reader (``teatree.llm.rate_limits``).

DB-free, network-free: every test drives a fake :class:`~teatree.llm.rate_limits.Transport`
returning a canned ``(status, headers)`` pair, so the parsing is verified against the
exact wire headers without a real ``/v1/messages`` call. A 200 and a 429 are asserted
to parse ALIKE (both carry the rate-limit headers); only a non-{200,429} status or a
transport error is a failure. The token is asserted to sign the request yet never
appear on the returned snapshot.
"""

import datetime as dt
import inspect
import json
from collections.abc import Mapping
from unittest.mock import patch

import httpx
import pytest

from teatree.llm.rate_limits import (
    MeteredKeySnapshot,
    ProbeResponse,
    RateLimitProbeError,
    RateLimitReader,
    RateLimitSnapshot,
    read_api_key_status,
    read_rate_limits,
)

_ORG = "anthropic-organization-id"
_RETRY_AFTER = "retry-after"
_5H_STATUS = "anthropic-ratelimit-unified-5h-status"
_5H_UTIL = "anthropic-ratelimit-unified-5h-utilization"
_5H_RESET = "anthropic-ratelimit-unified-5h-reset"
_7D_STATUS = "anthropic-ratelimit-unified-7d-status"
_7D_UTIL = "anthropic-ratelimit-unified-7d-utilization"
_7D_RESET = "anthropic-ratelimit-unified-7d-reset"

_REQUESTS_REMAINING = "anthropic-ratelimit-requests-remaining"
_REQUESTS_LIMIT = "anthropic-ratelimit-requests-limit"
_TOKENS_REMAINING = "anthropic-ratelimit-tokens-remaining"
_INPUT_TOKENS_REMAINING = "anthropic-ratelimit-input-tokens-remaining"
_OUTPUT_TOKENS_REMAINING = "anthropic-ratelimit-output-tokens-remaining"

_FULL_HEADERS = {
    _ORG: "org-abc123",
    _RETRY_AFTER: "42",
    _5H_STATUS: "allowed",
    _5H_UTIL: "0.30",
    _5H_RESET: "1782928800",  # Unix epoch seconds == 2026-07-01T18:00:00Z
    _7D_STATUS: "allowed_warning",
    _7D_UTIL: "0.80",
    _7D_RESET: "1783468800",  # Unix epoch seconds == 2026-07-08T00:00:00Z
}

_METERED_HEADERS = {
    _ORG: "org-metered",
    _REQUESTS_REMAINING: "4999",
    _REQUESTS_LIMIT: "5000",
    _TOKENS_REMAINING: "990000",
    _INPUT_TOKENS_REMAINING: "480000",
    _OUTPUT_TOKENS_REMAINING: "95000",
}

# The exact out-of-credits body the live API returns for a depleted metered key.
_CREDIT_BALANCE_BODY = json.dumps(
    {
        "type": "error",
        "error": {
            "type": "invalid_request_error",
            "message": "Your credit balance is too low to access the Anthropic API. "
            "You can purchase credits or upgrade in Plans & Billing.",
        },
    }
)
_OTHER_400_BODY = json.dumps(
    {"type": "error", "error": {"type": "invalid_request_error", "message": "max_tokens: must be >= 1"}}
)


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


def _read_api_key(status: int, headers: Mapping[str, str], *, body: str = "") -> MeteredKeySnapshot:
    transport = _RecordingTransport(ProbeResponse(status_code=status, headers=headers, body=body))
    return read_api_key_status("sk-ant-metered", transport=transport)


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

    def test_epoch_seconds_reset_parses_to_tz_aware_utc(self) -> None:
        # Anthropic sends the reset instant as Unix epoch seconds, not an ISO timestamp.
        snap = _read(200, {_5H_RESET: "1784476200"})
        assert snap.unified_5h_reset == dt.datetime(2026, 7, 19, 15, 50, tzinfo=dt.UTC)


class TestDefaultTransport:
    """The default ``httpx``-backed transport (used when no transport is injected)."""

    def test_default_transport_posts_via_httpx_and_folds_the_response(self) -> None:
        fake = httpx.Response(200, headers=_FULL_HEADERS, text="{}")
        with patch("teatree.llm.rate_limits.httpx.post", return_value=fake) as post_mock:
            snap = read_rate_limits("tok", is_oauth=False)
        assert post_mock.call_count == 1
        assert snap.organization_id == "org-abc123"


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


class TestRateLimitReaderProtocol:
    """The injected reader seam a selector depends on (``RateLimitReader``)."""

    def test_a_conforming_callable_satisfies_the_reader_seam(self) -> None:
        def fake(token: str, *, is_oauth: bool) -> RateLimitSnapshot:
            assert token
            assert is_oauth
            return RateLimitSnapshot.from_headers(_FULL_HEADERS)

        reader: RateLimitReader = fake
        assert reader("tok", is_oauth=True).organization_id == "org-abc123"

    def test_production_read_rate_limits_matches_the_reader_seam(self) -> None:
        reader: RateLimitReader = read_rate_limits
        params = inspect.signature(reader).parameters
        assert "token" in params
        assert params["is_oauth"].kind is inspect.Parameter.KEYWORD_ONLY


class TestMeteredApiKeyReader:
    def test_funded_key_parses_per_minute_headroom(self) -> None:
        snap = _read_api_key(200, _METERED_HEADERS)
        assert snap.out_of_credits is False
        assert snap.organization_id == "org-metered"
        assert snap.requests_remaining == 4999
        assert snap.requests_limit == 5000
        assert snap.tokens_remaining == 990000
        assert snap.input_tokens_remaining == 480000
        assert snap.output_tokens_remaining == 95000

    def test_missing_metered_headers_degrade_to_none(self) -> None:
        snap = _read_api_key(200, {})
        assert snap.out_of_credits is False
        assert snap.requests_remaining is None
        assert snap.tokens_remaining is None

    def test_credit_balance_400_body_maps_to_out_of_credits(self) -> None:
        snap = _read_api_key(400, {_ORG: "org-broke"}, body=_CREDIT_BALANCE_BODY)
        assert snap.out_of_credits is True
        assert snap.organization_id == "org-broke"
        assert snap.requests_remaining is None

    def test_credit_balance_match_is_case_insensitive(self) -> None:
        body = json.dumps({"error": {"message": "Your CREDIT BALANCE is too low."}})
        assert _read_api_key(400, {}, body=body).out_of_credits is True

    def test_other_400_without_credit_message_raises(self) -> None:
        with pytest.raises(RateLimitProbeError):
            _read_api_key(400, {}, body=_OTHER_400_BODY)

    def test_unparseable_400_body_raises(self) -> None:
        with pytest.raises(RateLimitProbeError):
            _read_api_key(400, {}, body="<html>gateway error</html>")

    @pytest.mark.parametrize("status", [401, 403, 429, 500, 529])
    def test_non_200_non_credit_status_raises(self, status: int) -> None:
        with pytest.raises(RateLimitProbeError):
            _read_api_key(status, _METERED_HEADERS)

    def test_transport_network_error_raises_probe_error(self) -> None:
        def boom(_headers: Mapping[str, str], _body: Mapping[str, object]) -> ProbeResponse:
            msg = "no route to host"
            raise httpx.ConnectError(msg)

        with pytest.raises(RateLimitProbeError):
            read_api_key_status("sk", transport=boom)

    def test_metered_probe_signs_with_x_api_key_not_bearer(self) -> None:
        transport = _RecordingTransport(ProbeResponse(status_code=200, headers=_METERED_HEADERS))
        snapshot = read_api_key_status("sk-ant-super-secret", transport=transport)
        assert transport.headers["x-api-key"] == "sk-ant-super-secret"
        assert "authorization" not in transport.headers
        assert "anthropic-beta" not in transport.headers
        assert "sk-ant-super-secret" not in repr(snapshot), "the key must never be carried on the snapshot"
