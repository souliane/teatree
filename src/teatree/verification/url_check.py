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

The address that passed that filter is then PINNED for the connection: the
transport is handed the vetted address rather than the hostname, so the second
DNS answer of a rebinding host — public on the guard's lookup, ``169.254.169.254``
on the transport's — never reaches a socket. ``Host`` and the TLS SNI / certificate
check stay bound to the URL's hostname, so pinning costs no verification.

The probe is HEAD-first (cheap) with a ranged-GET fallback for servers that
reject HEAD; the transport (``opener``) and host resolution (``resolver``) are
both injected so the check is exhaustively testable without network access.
"""

import http.client
import ipaddress
import socket
import ssl
import urllib.error
import urllib.request
from collections.abc import Callable
from dataclasses import dataclass
from enum import StrEnum
from functools import partial
from http.client import HTTPResponse
from urllib.parse import urlparse

DEFAULT_TIMEOUT = 8.0
_OK_MAX = 400  # 2xx/3xx resolve; 4xx/5xx do not.

Opener = Callable[[urllib.request.Request, float, str], HTTPResponse]
"""Transport seam: perform *request* with *timeout* against the vetted *address*."""

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


def _connect_pinned(
    pinned_address: str, target: tuple[str, int], timeout: float | None, source_address: tuple[str, int] | None
) -> socket.socket:
    """Open the socket to *pinned_address*, keeping only the port *target* asked for."""
    return socket.create_connection((pinned_address, target[1]), timeout, source_address)


class _PinnedHTTPConnection(http.client.HTTPConnection):
    """Plain HTTP to a vetted address; ``Host`` still carries the URL's own hostname."""

    def __init__(self, host: str, *, pinned_address: str, timeout: float | None = None) -> None:
        super().__init__(host, timeout=timeout)
        # ``HTTPConnection.__init__`` binds the socket factory per INSTANCE, so a subclass
        # method of the same name is shadowed and never reached — rebind it here instead.
        self._create_connection = partial(_connect_pinned, pinned_address)


class _PinnedHTTPSConnection(http.client.HTTPSConnection):
    """HTTPS to a vetted address; SNI and the certificate check stay on the hostname."""

    def __init__(
        self,
        host: str,
        *,
        pinned_address: str,
        timeout: float | None = None,
        context: ssl.SSLContext | None = None,
    ) -> None:
        super().__init__(host, timeout=timeout, context=context)
        self._create_connection = partial(_connect_pinned, pinned_address)


class _PinnedHTTPHandler(urllib.request.HTTPHandler):
    def __init__(self, pinned_address: str) -> None:
        super().__init__()
        self._connection = partial(_PinnedHTTPConnection, pinned_address=pinned_address)

    def http_open(self, req: urllib.request.Request) -> HTTPResponse:
        return self.do_open(self._connection, req)


class _PinnedHTTPSHandler(urllib.request.HTTPSHandler):
    def __init__(self, pinned_address: str) -> None:
        self._ssl_context = ssl.create_default_context()
        super().__init__(context=self._ssl_context)
        self._connection = partial(_PinnedHTTPSConnection, pinned_address=pinned_address, context=self._ssl_context)

    def https_open(self, req: urllib.request.Request) -> HTTPResponse:
        return self.do_open(self._connection, req)


def _default_opener(request: urllib.request.Request, timeout: float, address: str) -> HTTPResponse:
    opener = urllib.request.build_opener(
        _NoFollowRedirectHandler,
        _PinnedHTTPHandler(address),
        _PinnedHTTPSHandler(address),
    )
    return opener.open(request, timeout=timeout)


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


def _vet_host(url: str, host: str, *, resolver: HostResolver) -> tuple[str, UrlCheckResult | None]:
    """The address to pin the connection to, or a refusal result to return instead.

    A host that resolves to any loopback/private/link-local/reserved address is
    ``UNRESOLVED`` (the caller drops it — an internal-address citation is bogus).
    A resolution FAILURE is ``NETWORK_ERROR`` (teatree cannot tell, so the caller
    records rather than drops) — the same fail-open the transport already uses.
    """
    if not host:
        return "", UrlCheckResult(url, UrlCheckStatus.UNRESOLVED, detail="no host in URL")
    try:
        addresses = resolver(host)
    except (OSError, UnicodeError) as exc:
        return "", UrlCheckResult(url, UrlCheckStatus.NETWORK_ERROR, detail=f"DNS resolution failed: {exc}")
    if not addresses:
        return "", UrlCheckResult(url, UrlCheckStatus.NETWORK_ERROR, detail="host did not resolve")
    if any(_address_is_non_public(address) for address in addresses):
        detail = "refused: host resolves to a non-public address"
        return "", UrlCheckResult(url, UrlCheckStatus.UNRESOLVED, detail=detail)
    return addresses[0], None


def _probe(url: str, method: str, *, timeout: float, opener: Opener, address: str) -> UrlCheckResult:
    headers = {"Range": "bytes=0-0"} if method == "GET" else {}
    request = urllib.request.Request(url, method=method, headers=headers)  # noqa: S310 — scheme validated + host SSRF-filtered by the caller.
    try:
        with opener(request, timeout, address) as response:
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
    metadata/internal-service oracle. The vetted address is then handed to the
    transport, which connects to it rather than resolving the hostname a second
    time — the window a rebinding DNS answer needs. A HEAD that fails to resolve
    (4xx/5xx or a transport error) retries once as a ranged GET — many servers
    reject HEAD but serve GET — and the GET result governs, so a HEAD-hostile
    server does not produce a spurious ``UNRESOLVED``.
    """
    if not url.strip() or not url.lower().startswith(("http://", "https://")):
        return UrlCheckResult(url, UrlCheckStatus.NETWORK_ERROR, detail="unsupported or empty URL")
    address, refusal = _vet_host(url, urlparse(url).hostname or "", resolver=resolver)
    if refusal is not None:
        return refusal
    head = _probe(url, "HEAD", timeout=timeout, opener=opener, address=address)
    if head.ok:
        return head
    return _probe(url, "GET", timeout=timeout, opener=opener, address=address)
