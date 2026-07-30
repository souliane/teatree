"""Reusable connector-preflight probes derived from an overlay's declarations (#3333).

Core owns the preflight seam (:meth:`OverlayConnectors.preflight`) and the service
declarations (:meth:`OverlayConnectors.manifest` /
:meth:`OverlayConnectors.mcp_provider_expectations`) but shipped no probes to
connect them, so every overlay hand-wrote its own reachability code — ~200 lines
of transport classification, re-derived (and mis-derived) per overlay. The
dangerous failures are the fail-OPEN ones: a broken connector produces a quiet
no-op loop rather than an error.

This module is that probe library, written and tested ONCE:

:func:`is_transient` is the single transient/definitive taxonomy, decided in one
place so "DNS / connect / read-timeout / transient-5xx ⇒ transient; 4xx ⇒
definitive" is testable on its own.

:func:`reachability_probe` is a generic HTTP host-reachability probe encoding the
non-obvious inversion "any HTTP status proves the host is up; only a transport
failure is down".

:func:`standard_probes` builds live connector probes from the overlay's own
declarations, so :meth:`OverlayConnectors.preflight` can default to them and a
declared required connector becomes meaningful by itself.
"""

import logging
from collections.abc import Callable

import httpx

from teatree.core.connector_manifest import ConnectorRequirement, ConnectorUnavailableError, require_connector

logger = logging.getLogger(__name__)

_DEFAULT_TIMEOUT = 5.0

#: A ``5xx`` status is a transient server error; a ``4xx`` is a definitive rejection.
_SERVER_ERROR_MIN = 500


def is_transient(exc: BaseException) -> bool:
    """Classify a probe failure: transient (proves nothing) vs definitive.

    Transient — never hard-fail a healthy loop on these: a DNS failure, a
    connect / read / pool timeout, a network or protocol error, or a transient
    ``5xx`` HTTP status. Definitive — a ``4xx`` HTTP status (a live host that
    rejected the request), a malformed URL, or an unsupported scheme (a
    configuration bug, not a blip). Anything unrecognised is treated as
    definitive: an unexpected error is not something to silently fail open on.
    """
    if isinstance(exc, httpx.HTTPStatusError):
        return exc.response.status_code >= _SERVER_ERROR_MIN
    if isinstance(exc, httpx.TransportError):
        # Covers ConnectError (incl. DNS), the timeouts, and protocol/network
        # errors — all transient — EXCEPT an unsupported scheme, a config bug.
        return not isinstance(exc, httpx.UnsupportedProtocol)
    return False


def _raise_or_warn(*, required: bool, message: str) -> None:
    """Hard-fail a required connector; warn-and-continue for an optional one."""
    if required:
        raise RuntimeError(message)
    logger.warning("%s (optional connector — continuing degraded)", message)


def reachability_probe(
    *,
    name: str,
    url: str,
    required: bool = True,
    timeout: float = _DEFAULT_TIMEOUT,
) -> Callable[[], None]:
    """Return a zero-arg probe asserting *url*'s host is reachable.

    ANY HTTP status proves the host is up — for a pure reachability check a
    ``401`` / ``403`` is a SUCCESS signal (the host answered), so the probe passes
    on any response. A TRANSIENT transport failure proves nothing and never
    hard-fails (fail-open on a blip); a DEFINITIVE transport failure (a malformed
    URL or unsupported scheme) raises for a required connector and warns for an
    optional one.
    """

    def probe() -> None:
        try:
            httpx.get(url, timeout=timeout, follow_redirects=True)
        except (httpx.HTTPError, httpx.InvalidURL) as exc:
            if is_transient(exc):
                logger.warning("reachability probe for %r hit a transient error, not failing: %s", name, exc)
                return
            _raise_or_warn(required=required, message=f"connector {name!r} unreachable at {url}: {exc}")

    return probe


def _connectivity_probe(name: str, *, required: bool) -> Callable[[], None]:
    """A probe asserting the named MCP connector is connected (fail-open if unprobeable)."""

    def probe() -> None:
        try:
            require_connector(name)
        except ConnectorUnavailableError as exc:
            _raise_or_warn(required=required, message=str(exc))

    return probe


def standard_probes(
    manifest: list[ConnectorRequirement],
    expectations: dict[str, str],
) -> list[Callable[[], None]]:
    """Build connector probes from an overlay's own declarations.

    One live MCP-connectivity probe per server in
    :meth:`OverlayConnectors.mcp_provider_expectations`; required-ness comes from
    :meth:`OverlayConnectors.manifest` (a server also declared a REQUIRED connector
    hard-fails, otherwise it warns). Empty when the overlay declares no
    expectations — so an overlay that declares none keeps exactly its previous
    ``preflight()`` behaviour, and the claude.ai manifest classification stays
    owned by :func:`teatree.core.connector_preflight.assert_required_connectors`.
    """
    required_names = {req.name for req in manifest if req.required}
    return [_connectivity_probe(server, required=server in required_names) for server in expectations]


__all__ = ["is_transient", "reachability_probe", "standard_probes"]
