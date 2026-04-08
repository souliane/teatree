"""Overlay-aware backend factory — resolves config and builds backends.

This module bridges ``teatree.core`` (overlay) and ``teatree.backends``
(loader) so that callers in ``core`` and ``cli`` don't need to extract
tokens themselves.
"""

from teatree.backends.loader import (
    get_chat_notifier,
    get_ci_service,
    get_code_host,
    get_error_tracker,
    get_issue_tracker,
    reset_backend_caches,
)
from teatree.backends.protocols import CIService, CodeHost


def code_host_from_overlay() -> CodeHost | None:
    """Build a code-host backend using the active overlay's credentials."""
    from teatree.core.overlay_loader import get_overlay  # noqa: PLC0415

    try:
        overlay = get_overlay()
    except Exception:  # noqa: BLE001
        return None

    return get_code_host(
        github_token=overlay.config.get_github_token(),
        gitlab_token=overlay.config.get_gitlab_token(),
        gitlab_url=overlay.config.get_gitlab_url(),
    )


def ci_service_from_overlay() -> CIService | None:
    """Build a CI-service backend using the active overlay's credentials."""
    from teatree.core.overlay_loader import get_overlay  # noqa: PLC0415

    try:
        overlay = get_overlay()
    except Exception:  # noqa: BLE001
        return None

    return get_ci_service(
        gitlab_token=overlay.config.get_gitlab_token(),
        gitlab_url=overlay.config.get_gitlab_url(),
    )


__all__ = [
    "ci_service_from_overlay",
    "code_host_from_overlay",
    "get_chat_notifier",
    "get_ci_service",
    "get_code_host",
    "get_error_tracker",
    "get_issue_tracker",
    "reset_backend_caches",
]
