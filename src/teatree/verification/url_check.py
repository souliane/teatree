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

The URL is untrusted (it comes straight off a scanned article), so the probe is
an SSRF hazard: a naive fetch of ``http://169.254.169.254/…`` or
``http://localhost/…`` turns this existence oracle into a way to reach cloud
metadata or internal services. Before any request the host is resolved and every
resolved address is refused when it is loopback / private / link-local / reserved
(``UNRESOLVED`` — a non-public citation is dropped), and the probe never follows
redirects (a 3xx is itself an existence signal, and NOT following it means a
redirect to an internal address is never fetched).

The probe is HEAD-first (cheap) with a ranged-GET fallback for servers that
reject HEAD; the transport (``opener``) and host resolution (``resolver``) are
both injected so the check is exhaustively testable without network access.
"""

import ipaddress
import socket
import urllib.error
import urllib.request
from collections.abc import Callable
from dataclasses import dataclass
from enum import StrEnum
from http.client import HTTPResponse
from urllib.parse import urlparse

DEFAULT_TIMEOUT = 8.0
_OK_MAX = 400  # 2xx/3xx resolve; 4xx/5xx do not.

Opener = Callable[[urllib.request.Request, float], HTTPResponse]
"""Transport seam: perform *request* with *timeout*, return the response."""

HostResolver = Callable[[str], list[str]]
"""SSRF-guard seam: resolve *host* to its list of IP-address strings."""


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


class _NoFollowRedirectHandler(urllib.request.HTTPRedirectHandler):
    """Refuse to follow redirects, so a 3xx is surfaced instead of chased.

    Returning ``None`` from ``redirect_request`` leaves the 3xx to propagate as
    an ``HTTPError`` (which :func:`_probe` reads as an "exists" signal). The
    redirect target — potentially an internal address a public host 302s to — is
    never fetched, closing the redirect-based SSRF bypass.
    """

    def redirect_request(self, *_args: object, **_kwargs: object) -> None:  # noqa: PLR6301 — overrides HTTPRedirectHandler.redirect_request (instance-method contract)
        return None


_NO_REDIRECT_OPENER = urllib.request.build_opener(_NoFollowRedirectHandler)


def _default_opener(request: urllib.request.Request, timeout: float) -> HTTPResponse:
    return _NO_REDIRECT_OPENER.open(request, timeout=timeout)


def _default_resolver(host: str) -> list[str]:
    return [str(info[4][0]) for info in socket.getaddrinfo(host, None)]


def _address_is_non_public(ip_str: str) -> bool:
    """Whether *ip_str* is an address the probe must refuse to reach.

    An unparsable value is refused conservatively — if teatree cannot classify
    the address it must not fetch it.
    """
    try:
        ip = ipaddress.ip_address(ip_str)
    except ValueError:
        return True
    return ip.is_private or ip.is_loopback or ip.is_link_local or ip.is_reserved or ip.is_multicast or ip.is_unspecified


def _reject_non_public_host(url: str, host: str, *, resolver: HostResolver) -> UrlCheckResult | None:
    """Return a refusal result for a non-public host, or ``None`` to proceed.

    A host that resolves to any loopback/private/link-local/reserved address is
    ``UNRESOLVED`` (the caller drops it — an internal-address citation is bogus).
    A resolution FAILURE is ``NETWORK_ERROR`` (teatree cannot tell, so the caller
    records rather than drops) — the same fail-open the transport already uses.
    """
    if not host:
        return UrlCheckResult(url, UrlCheckStatus.UNRESOLVED, detail="no host in URL")
    try:
        addresses = resolver(host)
    except (OSError, UnicodeError) as exc:
        return UrlCheckResult(url, UrlCheckStatus.NETWORK_ERROR, detail=f"DNS resolution failed: {exc}")
    if not addresses:
        return UrlCheckResult(url, UrlCheckStatus.NETWORK_ERROR, detail="host did not resolve")
    if any(_address_is_non_public(address) for address in addresses):
        return UrlCheckResult(url, UrlCheckStatus.UNRESOLVED, detail="refused: host resolves to a non-public address")
    return None


def _probe(url: str, method: str, *, timeout: float, opener: Opener) -> UrlCheckResult:
    headers = {"Range": "bytes=0-0"} if method == "GET" else {}
    request = urllib.request.Request(url, method=method, headers=headers)  # noqa: S310 — scheme validated + host SSRF-filtered by the caller.
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


def check_url(
    url: str,
    *,
    timeout: float = DEFAULT_TIMEOUT,
    opener: Opener = _default_opener,
    resolver: HostResolver = _default_resolver,
) -> UrlCheckResult:
    """Probe whether *url* resolves. HEAD first, ranged-GET fallback.

    Only ``http``/``https`` URLs are probed; any other scheme (or a blank URL) is
    an immediate ``NETWORK_ERROR`` (teatree cannot verify it, so the caller must
    not drop on it). The host is SSRF-filtered BEFORE any request: a host that
    resolves to a loopback/private/link-local/reserved address is refused
    (``UNRESOLVED``), so the untrusted URL can never turn the probe into a
    metadata/internal-service oracle. A HEAD that fails to resolve (4xx/5xx or a
    transport error) retries once as a ranged GET — many servers reject HEAD but
    serve GET — and the GET result governs, so a HEAD-hostile server does not
    produce a spurious ``UNRESOLVED``.
    """
    if not url.strip() or not url.lower().startswith(("http://", "https://")):
        return UrlCheckResult(url, UrlCheckStatus.NETWORK_ERROR, detail="unsupported or empty URL")
    refusal = _reject_non_public_host(url, urlparse(url).hostname or "", resolver=resolver)
    if refusal is not None:
        return refusal
    head = _probe(url, "HEAD", timeout=timeout, opener=opener)
    if head.ok:
        return head
    return _probe(url, "GET", timeout=timeout, opener=opener)
