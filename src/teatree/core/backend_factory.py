"""Overlay-aware backend factory — resolves config and builds backends.

This module bridges ``teatree.core`` (overlay registry) and
``teatree.backends`` (loader) so that callers in ``core`` and ``cli`` don't
need to extract tokens or branch on platform themselves.
"""

from dataclasses import dataclass
from functools import lru_cache

from django.core.exceptions import ImproperlyConfigured

from teatree.backends.loader import (
    get_ci_service,
    get_code_host,
    get_messaging,
)
from teatree.backends.loader import (
    reset_backend_caches as _reset_loader_caches,
)
from teatree.backends.protocols import CIService, CodeHostBackend, MessagingBackend
from teatree.core.overlay_loader import get_all_overlays, get_overlay


@dataclass(frozen=True, slots=True)
class OverlayBackends:
    """Backends and config slice for one registered overlay.

    The loop tick builds one set of scanners per ``OverlayBackends`` so a
    user with multiple overlays (e.g. one per GitHub identity) sees PRs,
    issues, and Slack mentions from all of them in one statusline.
    """

    name: str
    host: CodeHostBackend | None
    messaging: MessagingBackend | None
    ready_labels: tuple[str, ...]


@lru_cache(maxsize=1)
def code_host_from_overlay() -> CodeHostBackend | None:
    """Build a code-host backend using the active overlay's credentials.

    Cached for the loop tick — every scanner that needs the host shares one
    instance per process. Tests that swap overlays must call
    :func:`reset_backend_caches` to discard the cached client.
    """
    try:
        overlay = get_overlay()
    except ImproperlyConfigured:
        return None
    return get_code_host(overlay)


@lru_cache(maxsize=1)
def messaging_from_overlay() -> MessagingBackend | None:
    """Build a messaging backend using the active overlay's config (cached)."""
    try:
        overlay = get_overlay()
    except ImproperlyConfigured:
        return None
    return get_messaging(overlay)


def ci_service_from_overlay() -> CIService | None:
    """Build a CI-service backend using the active overlay's credentials."""
    try:
        overlay = get_overlay()
    except ImproperlyConfigured:
        return None

    return get_ci_service(
        gitlab_token=overlay.config.get_gitlab_token(),
        gitlab_url=overlay.config.gitlab_url,
    )


def iter_overlay_backends() -> list[OverlayBackends]:
    """Yield :class:`OverlayBackends` for every registered overlay.

    Overlays whose credentials don't resolve get ``host=None`` /
    ``messaging=None`` — the caller decides whether to skip them.
    """
    out: list[OverlayBackends] = []
    for name, overlay in get_all_overlays().items():
        try:
            host = get_code_host(overlay)
        except (ImproperlyConfigured, ValueError):
            host = None
        try:
            messaging = get_messaging(overlay)
        except (ImproperlyConfigured, ValueError):
            messaging = None
        out.append(
            OverlayBackends(
                name=name,
                host=host,
                messaging=messaging,
                ready_labels=tuple(overlay.config.ready_labels),
            )
        )
    return out


def reset_backend_caches() -> None:
    """Clear all per-overlay backend caches.

    Call when the active overlay changes (overlay reload, multi-overlay
    test fixtures) so the next factory call rebuilds with fresh credentials.
    """
    code_host_from_overlay.cache_clear()
    messaging_from_overlay.cache_clear()
    _reset_loader_caches()


__all__ = [
    "OverlayBackends",
    "ci_service_from_overlay",
    "code_host_from_overlay",
    "get_ci_service",
    "get_code_host",
    "get_messaging",
    "iter_overlay_backends",
    "messaging_from_overlay",
    "reset_backend_caches",
]
