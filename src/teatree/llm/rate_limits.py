r"""Read an Anthropic token's rate-limit health (``teatree.llm.rate_limits``).

FOUNDATION-pure: HTTP + header parsing only — no DB, no teatree-domain import. One
tiny ``POST /v1/messages`` (``claude-haiku-4-5``, ``max_tokens=1``) is signed with the
token and its response is folded into a frozen result.

Two credential shapes are probed differently, because Anthropic reports their headroom
differently:

*   A **subscription OAuth** token (``authorization: Bearer`` + ``anthropic-beta:
    oauth-2025-04-20``) emits the ``anthropic-ratelimit-unified-{5h,7d}-*`` headers.
    :func:`read_rate_limits` folds them into a :class:`RateLimitSnapshot`; a **429 still
    carries these headers**, so a 200 and a 429 are treated ALIKE.
*   A **metered API key** (``x-api-key`` — NO ``Bearer``, NO oauth beta) does NOT emit
    the unified windows. A funded key returns 200 with the standard per-minute
    ``anthropic-ratelimit-{requests,tokens}-*`` headers; an out-of-credits key returns a
    400 whose body says the credit balance is too low. :func:`read_api_key_status` folds
    those into a :class:`MeteredKeySnapshot` (funded vs out-of-credits + per-minute
    headroom) — the exact prepaid dollar balance is NOT available to a standard key.

For both, only a network error or an unexpected status is a failure
(:class:`RateLimitProbeError`). The token is used ONLY to sign the request header — it is
never logged and never stored on the returned result. The HTTP call is injected
(:class:`Transport`) so tests drive canned ``(status, headers, body)`` triples with no
real network; the default transport uses ``httpx``.

The header names follow Anthropic's response-header conventions and are centralised as
module constants so a naming drift is a one-line fix. ``utilization`` is a 0.0-1.0
fraction; ``*-reset`` is Unix epoch seconds parsed to a tz-aware UTC ``datetime``;
``retry-after`` is whole seconds.
"""

import datetime as dt
import json
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

# A metered API key that is out of credits answers the probe with a 400 whose body
# names the credit balance — matched on the message text, NOT the bare status (other
# 400s exist), so only a genuine credit-balance error becomes OUT_OF_CREDITS.
_CREDIT_ERROR_STATUS = 400
_CREDIT_BALANCE_MARKER = "credit balance"

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

# Metered API-key per-minute headers (a funded key emits these on the 200 probe).
_REQUESTS_REMAINING = "anthropic-ratelimit-requests-remaining"
_REQUESTS_LIMIT = "anthropic-ratelimit-requests-limit"
_TOKENS_REMAINING = "anthropic-ratelimit-tokens-remaining"
_INPUT_TOKENS_REMAINING = "anthropic-ratelimit-input-tokens-remaining"
_OUTPUT_TOKENS_REMAINING = "anthropic-ratelimit-output-tokens-remaining"


class RateLimitProbeError(RuntimeError):
    """The probe could not read the account's rate-limit headers.

    Raised for a transport/network failure or a non-{200,429} error status — the
    signals that carry NO usable rate-limit headers. A 429 is NOT an error here:
    it carries the headers and yields a normal snapshot.
    """


@dataclass(frozen=True)
class ProbeResponse:
    """The status code, response headers, and body text of one rate-limit probe.

    *body* is the raw response text — read only on the metered path to detect the
    out-of-credits ``400`` (the OAuth path never needs it), so it defaults to ``""``.
    """

    status_code: int
    headers: Mapping[str, str]
    body: str = ""


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
        lower = _lower_headers(headers)
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


@dataclass(frozen=True)
class MeteredKeySnapshot:
    """A metered API key's credit state + per-minute rate-limit headroom.

    A metered key does NOT emit the unified ``*-5h`` / ``*-7d`` windows an OAuth
    subscription does. A funded key (HTTP 200) reports the standard per-minute
    ``anthropic-ratelimit-{requests,tokens}-*`` headers; an out-of-credits key is
    signalled by the 400 "credit balance is too low" body. The exact prepaid dollar
    balance is not exposed to a standard key, so :attr:`out_of_credits` (funded vs
    depleted) is the coarsest feasible credit signal. Token-free, like
    :class:`RateLimitSnapshot`: an ``int | None`` field is ``None`` when its header is
    absent.
    """

    organization_id: str
    out_of_credits: bool
    requests_remaining: int | None
    requests_limit: int | None
    tokens_remaining: int | None
    input_tokens_remaining: int | None
    output_tokens_remaining: int | None

    @classmethod
    def funded(cls, headers: Mapping[str, str]) -> "MeteredKeySnapshot":
        """A funded key's per-minute headroom, parsed from the 200 response headers."""
        lower = _lower_headers(headers)
        return cls(
            organization_id=lower.get(_ORG_ID, ""),
            out_of_credits=False,
            requests_remaining=_parse_int(lower.get(_REQUESTS_REMAINING)),
            requests_limit=_parse_int(lower.get(_REQUESTS_LIMIT)),
            tokens_remaining=_parse_int(lower.get(_TOKENS_REMAINING)),
            input_tokens_remaining=_parse_int(lower.get(_INPUT_TOKENS_REMAINING)),
            output_tokens_remaining=_parse_int(lower.get(_OUTPUT_TOKENS_REMAINING)),
        )

    @classmethod
    def depleted(cls, headers: Mapping[str, str]) -> "MeteredKeySnapshot":
        """An out-of-credits key: only the org id survives; no per-minute headroom."""
        return cls(
            organization_id=_lower_headers(headers).get(_ORG_ID, ""),
            out_of_credits=True,
            requests_remaining=None,
            requests_limit=None,
            tokens_remaining=None,
            input_tokens_remaining=None,
            output_tokens_remaining=None,
        )


class RateLimitReader(Protocol):
    """The reader seam a routing selector injects — :func:`read_rate_limits` satisfies it.

    Selectors depend on this narrow ``(token, *, is_oauth) -> RateLimitSnapshot``
    surface so a test can map a token to a canned snapshot without the HTTP layer,
    while production passes the real :func:`read_rate_limits`.
    """

    def __call__(self, token: str, *, is_oauth: bool) -> RateLimitSnapshot: ...


#: The metered-key reader seam a routing selector / reporter injects —
#: :func:`read_api_key_status` satisfies it. A metered key has no ``is_oauth`` axis
#: (it is always ``x-api-key``), so the seam is a plain ``(token) -> MeteredKeySnapshot``.
type MeteredKeyReader = Callable[[str], MeteredKeySnapshot]


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


def read_api_key_status(token: str, *, transport: Transport | None = None) -> MeteredKeySnapshot:
    """Probe a metered API *key* once and return its :class:`MeteredKeySnapshot`.

    A metered key authenticates with ``x-api-key`` (no ``Bearer``, no oauth beta) and
    does NOT emit the unified windows: a funded key returns 200 with the per-minute
    headers, an out-of-credits key returns a 400 whose body says the credit balance is
    too low (matched on the message, not the bare status). Any other status — or a
    transport error — raises :class:`RateLimitProbeError`. The key signs the request and
    is never logged.
    """
    call = transport or _httpx_transport
    try:
        response = call(_api_key_headers(token), _PROBE_BODY)
    except httpx.HTTPError as exc:
        msg = f"api-key probe transport failed: {type(exc).__name__}"
        raise RateLimitProbeError(msg) from exc
    if response.status_code == _OK:
        return MeteredKeySnapshot.funded(response.headers)
    if response.status_code == _CREDIT_ERROR_STATUS and _is_credit_balance_error(response.body):
        return MeteredKeySnapshot.depleted(response.headers)
    msg = f"api-key probe returned status {response.status_code}"
    raise RateLimitProbeError(msg)


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


def _api_key_headers(token: str) -> dict[str, str]:
    return {
        "x-api-key": token,
        "anthropic-version": _ANTHROPIC_VERSION,
        "content-type": "application/json",
    }


def _is_credit_balance_error(body: str) -> bool:
    """Whether a probe body is the metered "credit balance is too low" 400 error."""
    try:
        payload = json.loads(body)
    except (ValueError, TypeError):
        return False
    error = payload.get("error") if isinstance(payload, dict) else None
    message = error.get("message", "") if isinstance(error, dict) else ""
    return _CREDIT_BALANCE_MARKER in str(message).lower()


def _httpx_transport(headers: Mapping[str, str], body: Mapping[str, object]) -> ProbeResponse:
    response = httpx.post(_API_URL, headers=dict(headers), json=dict(body), timeout=_PROBE_TIMEOUT_SECONDS)
    return ProbeResponse(status_code=response.status_code, headers=response.headers, body=response.text)


def _lower_headers(headers: Mapping[str, str]) -> dict[str, str]:
    return {key.lower(): value for key, value in headers.items()}


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
    """Fold a unified-window ``*-reset`` header into a tz-aware UTC ``datetime``.

    Anthropic emits the reset instant as Unix epoch **seconds** (e.g. ``"1784476200"``),
    NOT an RFC 3339 timestamp; ``None`` when the header is absent or not an integer.
    """
    if not raw:
        return None
    try:
        epoch_seconds = int(raw)
    except ValueError:
        return None
    return dt.datetime.fromtimestamp(epoch_seconds, tz=dt.UTC)
