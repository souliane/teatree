"""Deterministic URL existence check (PR-15, M5).

A cited URL — a scanned-news article link, a referenced source — must actually
resolve before teatree records it, or the backlog fills with fabricated / 404
citations. :func:`check_url` probes the URL and returns a typed result with three
outcomes the caller must treat differently. ``OK`` means the server answered
2xx/3xx (the URL exists). ``UNRESOLVED`` means it answered 4xx/5xx (the URL does
not exist) — the caller DROPS the candidate. ``NETWORK_ERROR`` means a timeout /
DNS / connection failure left teatree unable to tell — the caller must NOT drop a
possibly-valid URL on its own transient failure, so it records anyway and
surfaces the error distinctly.

The probe is HEAD-first (cheap) with a ranged-GET fallback for servers that
reject HEAD, and the transport is injected (``opener``) so the check is
exhaustively testable without network access.
"""

import urllib.error
import urllib.request
from collections.abc import Callable
from dataclasses import dataclass
from enum import StrEnum
from http.client import HTTPResponse

DEFAULT_TIMEOUT = 8.0
_OK_MAX = 400  # 2xx/3xx resolve; 4xx/5xx do not.

Opener = Callable[[urllib.request.Request, float], HTTPResponse]
"""Transport seam: perform *request* with *timeout*, return the response."""


class UrlCheckStatus(StrEnum):
    """The three distinct outcomes of an existence probe."""

    OK = "ok"
    UNRESOLVED = "unresolved"
    NETWORK_ERROR = "network-error"


@dataclass(frozen=True, slots=True)
class UrlCheckResult:
    """Outcome of :func:`check_url` — status plus the HTTP code / failure detail."""

    url: str
    status: UrlCheckStatus
    http_status: int | None = None
    detail: str = ""

    @property
    def ok(self) -> bool:
        return self.status is UrlCheckStatus.OK


def _default_opener(request: urllib.request.Request, timeout: float) -> HTTPResponse:
    return urllib.request.urlopen(request, timeout=timeout)  # noqa: S310 — http(s) existence probe of an externally-cited URL, method-restricted below.


def _probe(url: str, method: str, *, timeout: float, opener: Opener) -> UrlCheckResult:
    headers = {"Range": "bytes=0-0"} if method == "GET" else {}
    request = urllib.request.Request(url, method=method, headers=headers)  # noqa: S310 — scheme validated by the caller.
    try:
        with opener(request, timeout) as response:
            code = int(response.status)
    except urllib.error.HTTPError as exc:
        code = int(exc.code)
        status = UrlCheckStatus.OK if code < _OK_MAX else UrlCheckStatus.UNRESOLVED
        return UrlCheckResult(url, status, http_status=code, detail=f"HTTP {code}")
    except (urllib.error.URLError, TimeoutError, OSError, ValueError) as exc:
        return UrlCheckResult(url, UrlCheckStatus.NETWORK_ERROR, detail=str(exc))
    status = UrlCheckStatus.OK if code < _OK_MAX else UrlCheckStatus.UNRESOLVED
    return UrlCheckResult(url, status, http_status=code, detail=f"HTTP {code}")


def check_url(url: str, *, timeout: float = DEFAULT_TIMEOUT, opener: Opener = _default_opener) -> UrlCheckResult:
    """Probe whether *url* resolves. HEAD first, ranged-GET fallback.

    Only ``http``/``https`` URLs are probed; any other scheme (or a blank URL) is
    an immediate ``NETWORK_ERROR`` (teatree cannot verify it, so the caller must
    not drop on it). A HEAD that fails to resolve (4xx/5xx or a transport error)
    retries once as a ranged GET — many servers reject HEAD but serve GET — and
    the GET result governs, so a HEAD-hostile server does not produce a spurious
    ``UNRESOLVED``.
    """
    if not url.strip() or not url.lower().startswith(("http://", "https://")):
        return UrlCheckResult(url, UrlCheckStatus.NETWORK_ERROR, detail="unsupported or empty URL")
    head = _probe(url, "HEAD", timeout=timeout, opener=opener)
    if head.ok:
        return head
    return _probe(url, "GET", timeout=timeout, opener=opener)
