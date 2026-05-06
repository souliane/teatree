"""Overlay-aware backend factory — resolves config and builds backends.

This module bridges ``teatree.core`` (overlay registry) and
``teatree.backends`` (loader) so that callers in ``core`` and ``cli`` don't
need to extract tokens or branch on platform themselves.
"""

from django.core.exceptions import ImproperlyConfigured

from teatree.backends.loader import (
    get_ci_service,
    get_code_host,
    get_messaging,
    reset_backend_caches,
)
from teatree.backends.protocols import CIService, CodeHostBackend, MessagingBackend
from teatree.core.overlay_loader import get_overlay


def code_host_from_overlay() -> CodeHostBackend | None:
    """Build a code-host backend using the active overlay's credentials."""
    try:
        overlay = get_overlay()
    except ImproperlyConfigured:
        return None
    return get_code_host(overlay)


def messaging_from_overlay() -> MessagingBackend | None:
    """Build a messaging backend using the active overlay's config."""
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


__all__ = [
    "ci_service_from_overlay",
    "code_host_from_overlay",
    "get_ci_service",
    "get_code_host",
    "get_messaging",
    "messaging_from_overlay",
    "reset_backend_caches",
]
