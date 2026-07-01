r"""Read an Anthropic token's unified rate-limit health (``teatree.llm.rate_limits``).

FOUNDATION-pure: HTTP + header parsing only — no DB, no teatree-domain import. One
tiny ``POST /v1/messages`` (``claude-haiku-4-5``, ``max_tokens=1``) returns the
account's unified rate-limit headers, which :func:`read_rate_limits` folds into a
frozen :class:`RateLimitSnapshot`. A **429 still carries these headers**, so a 200
and a 429 are treated ALIKE (both yield a snapshot); only a network error or a
non-429 error status is a failure (:class:`RateLimitProbeError`).

The token is used ONLY to sign the request header — it is never logged and never
stored on the returned snapshot. The HTTP call is injected (:class:`Transport`) so
tests drive canned ``(status, headers)`` pairs with no real network; the default
transport uses ``httpx``.

The header names follow Anthropic's ``anthropic-ratelimit-unified-<window>-<field>``
convention and are centralised as module constants so a naming drift is a one-line
fix. ``utilization`` is a 0.0-1.0 fraction; ``*-reset`` is an RFC 3339 timestamp
parsed to a tz-aware UTC ``datetime``; ``retry-after`` is whole seconds.
"""

import datetime as dt
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import Protocol

import httpx

_API_URL = "https://api.anthropic.com/v1/messages"
_PROBE_MODEL = "claude-haiku-4-5"
_ANTHROPIC_VERSION = "2023-06-01"
_OAUTH_BETA = "oauth-2025-04-20"
_PROBE_TIMEOUT_SECONDS = 20.0

# The two response statuses that carry rate-limit headers: a healthy 200 and a
# throttled 429 both report the account's current unified windows.
_OK = 200
_RATE_LIMITED = 429

# Unified rate-limit response headers (lower-cased; the reader lower-cases every
# incoming header key so a case-variant or an httpx.Headers both resolve).
_ORG_ID = "anthropic-organization-id"
_RETRY_AFTER = "retry-after"
_5H_STATUS = "anthropic-ratelimit-unified-5h-status"
_5H_UTILIZATION = "anthropic-ratelimit-unified-5h-utilization"
_5H_RESET = "anthropic-ratelimit-unified-5h-reset"
_7D_STATUS = "anthropic-ratelimit-unified-7d-status"
_7D_UTILIZATION = "anthropic-ratelimit-unified-7d-utilization"
_7D_RESET = "anthropic-ratelimit-unified-7d-reset"


class RateLimitProbeError(RuntimeError):
    """The probe could not read the account's rate-limit headers.

    Raised for a transport/network failure or a non-{200,429} error status — the
    signals that carry NO usable rate-limit headers. A 429 is NOT an error here:
    it carries the headers and yields a normal snapshot.
    """


@dataclass(frozen=True)
class ProbeResponse:
    """The status code + response headers of one rate-limit probe (no body)."""

    status_code: int
    headers: Mapping[str, str]


#: The injected HTTP seam. Given the request headers + JSON body, perform the
#: ``POST /v1/messages`` and return its status + response headers. The default is
#: :func:`_httpx_transport`; tests pass a fake returning canned pairs.
type Transport = Callable[[Mapping[str, str], Mapping[str, object]], ProbeResponse]


@dataclass(frozen=True)
class RateLimitSnapshot:
    """One account's unified rate-limit health, parsed from the response headers.

    Deliberately token-free: the credential value that signed the probe is never
    carried here. ``*_status`` is the raw status word (e.g. ``allowed`` /
    ``rejected``); ``*_utilization`` is a 0.0-1.0 fraction; ``*_reset`` is a
    tz-aware UTC datetime (``None`` when the header is absent/unparsable);
    ``retry_after`` is whole seconds (``None`` when absent).
    """

    organization_id: str
    unified_5h_status: str
    unified_5h_utilization: float
    unified_5h_reset: dt.datetime | None
    unified_7d_status: str
    unified_7d_utilization: float
    unified_7d_reset: dt.datetime | None
    retry_after: int | None

    @classmethod
    def from_headers(cls, headers: Mapping[str, str]) -> "RateLimitSnapshot":
        """Fold a case-insensitive header map into the snapshot (missing → defaults)."""
        lower = {key.lower(): value for key, value in headers.items()}
        return cls(
            organization_id=lower.get(_ORG_ID, ""),
            unified_5h_status=lower.get(_5H_STATUS, ""),
            unified_5h_utilization=_parse_fraction(lower.get(_5H_UTILIZATION)),
            unified_5h_reset=_parse_reset(lower.get(_5H_RESET)),
            unified_7d_status=lower.get(_7D_STATUS, ""),
            unified_7d_utilization=_parse_fraction(lower.get(_7D_UTILIZATION)),
            unified_7d_reset=_parse_reset(lower.get(_7D_RESET)),
            retry_after=_parse_int(lower.get(_RETRY_AFTER)),
        )


class RateLimitReader(Protocol):
    """The reader seam a routing selector injects — :func:`read_rate_limits` satisfies it.

    Selectors depend on this narrow ``(token, *, is_oauth) -> RateLimitSnapshot``
    surface so a test can map a token to a canned snapshot without the HTTP layer,
    while production passes the real :func:`read_rate_limits`.
    """

    def __call__(self, token: str, *, is_oauth: bool) -> RateLimitSnapshot: ...


def read_rate_limits(
    token: str,
    *,
    is_oauth: bool,
    transport: Transport | None = None,
) -> RateLimitSnapshot:
    """Probe *token*'s account once and return its :class:`RateLimitSnapshot`.

    *is_oauth* adds the ``anthropic-beta: oauth-2025-04-20`` header the subscription
    OAuth token needs (a metered API key omits it). A 200 or a 429 both parse to a
    snapshot; any other status or a transport error raises
    :class:`RateLimitProbeError`. The token signs the request and is never logged.
    """
    call = transport or _httpx_transport
    try:
        response = call(_request_headers(token, is_oauth=is_oauth), _PROBE_BODY)
    except httpx.HTTPError as exc:
        msg = f"rate-limit probe transport failed: {type(exc).__name__}"
        raise RateLimitProbeError(msg) from exc
    if response.status_code not in {_OK, _RATE_LIMITED}:
        msg = f"rate-limit probe returned status {response.status_code}"
        raise RateLimitProbeError(msg)
    return RateLimitSnapshot.from_headers(response.headers)


_PROBE_BODY: Mapping[str, object] = {
    "model": _PROBE_MODEL,
    "max_tokens": 1,
    "messages": [{"role": "user", "content": "."}],
}


def _request_headers(token: str, *, is_oauth: bool) -> dict[str, str]:
    headers = {
        "authorization": f"Bearer {token}",
        "anthropic-version": _ANTHROPIC_VERSION,
        "content-type": "application/json",
    }
    if is_oauth:
        headers["anthropic-beta"] = _OAUTH_BETA
    return headers


def _httpx_transport(headers: Mapping[str, str], body: Mapping[str, object]) -> ProbeResponse:
    response = httpx.post(_API_URL, headers=dict(headers), json=dict(body), timeout=_PROBE_TIMEOUT_SECONDS)
    return ProbeResponse(status_code=response.status_code, headers=response.headers)


def _parse_fraction(raw: str | None) -> float:
    if not raw:
        return 0.0
    try:
        return float(raw)
    except ValueError:
        return 0.0


def _parse_int(raw: str | None) -> int | None:
    if not raw:
        return None
    try:
        return int(raw)
    except ValueError:
        return None


def _parse_reset(raw: str | None) -> dt.datetime | None:
    if not raw:
        return None
    try:
        parsed = dt.datetime.fromisoformat(raw)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=dt.UTC)
    return parsed.astimezone(dt.UTC)
