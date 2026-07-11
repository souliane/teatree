"""The one declaring-overlay → configured-client resolver for the MCP service groups.

Every per-service tool group (forge / slack / notion / sentry) needs the same
thing: the first registered overlay that both declares the group's
:class:`~teatree.backends.types.Service` in ``required_third_party_services`` AND
has a configured client for it. The client is built by that service's
``backend_factory`` seam (``code_host_from_overlay`` / ``messaging_from_overlay``
/ ``notion_client_from_overlay`` / ``sentry_client_from_overlay``), which returns
``None`` when the overlay declares the service but has no credentials.

Overlay discovery rides the ``lru_cache``-backed ``_discover_overlays`` and each
factory's own per-overlay cache, so a per-call resolve is a cache hit, not a
rebuild. Each service keeps a thin named ``_client`` / ``_forge_client`` wrapper
(the test patch seam) that delegates here.
"""

from collections.abc import Callable

from teatree.backends.types import Service
from teatree.core.overlay_loader import get_all_overlays


def resolve_declaring_overlay_client[Client](
    service: Service,
    build: Callable[[str], Client | None],
    *,
    description: str,
) -> Client:
    """Return the first declaring overlay's configured client for *service*.

    Raises ``RuntimeError`` naming *description* when no registered overlay
    declares *service* with a configured client.
    """
    for name, overlay in get_all_overlays().items():
        if service in overlay.config.required_third_party_services:
            client = build(name)
            if client is not None:
                return client
    msg = f"No registered overlay declares a configured {description}"
    raise RuntimeError(msg)
