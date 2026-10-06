"""The one declaring-overlay → configured-client resolver for the MCP service groups.

Every per-service tool group (forge / slack / notion / sentry / sharepoint) needs the
same thing: the first registered overlay that both declares the group's
:class:`~teatree.backends.types.Service` in ``required_third_party_services`` AND has a
configured client for it. :data:`SERVICE_CLIENTS` names, per service, the
``backend_factory`` builder that answers it — ``None`` when the overlay declares the
service but has no credentials — and what a missing client is called. The tool groups
and ``t3 doctor``'s declared-services check resolve through the same entries, so they
agree on what "configured" means.

Overlay discovery rides the ``lru_cache``-backed ``_discover_overlays`` and each
factory's own per-overlay cache, so a per-call resolve is a cache hit, not a
rebuild. Each service keeps a thin named ``_client`` / ``_forge_client`` wrapper
(the test patch seam) that delegates here.
"""

from collections.abc import Callable
from dataclasses import dataclass

from mcp.server.mcpserver.exceptions import ToolError

from teatree.backends.types import Service
from teatree.core.backend_factory import (
    code_host_from_overlay,
    configured_messaging_from_overlay,
    notion_client_from_overlay,
    sentry_client_from_overlay,
    sharepoint_client_from_overlay,
)
from teatree.core.overlay_loader import get_all_overlays


def resolve_declaring_overlay_client[Client](
    service: Service,
    build: Callable[[str], Client | None],
    *,
    description: str,
) -> Client:
    """Return the first declaring overlay's configured client for *service*.

    Raises ``ToolError`` naming *description* when no registered overlay
    declares *service* with a configured client.
    """
    for name, overlay in get_all_overlays().items():
        if service in overlay.config.required_third_party_services:
            client = build(name)
            if client is not None:
                return client
    msg = f"No registered overlay declares a configured {description}"
    raise ToolError(msg)


def declaring_overlays() -> dict[Service, list[str]]:
    declarers: dict[Service, list[str]] = {}
    for name, overlay in get_all_overlays().items():
        for service in overlay.config.required_third_party_services:
            declarers.setdefault(service, []).append(name)
    return declarers


@dataclass(frozen=True, slots=True)
class ServiceClient[Client]:
    service: Service
    build: Callable[[str], Client | None]
    description: str

    def resolve(self) -> Client:
        return resolve_declaring_overlay_client(self.service, self.build, description=self.description)


GITHUB = ServiceClient(Service.GITHUB, code_host_from_overlay, "github code host")
GITLAB = ServiceClient(Service.GITLAB, code_host_from_overlay, "gitlab code host")
# The configured variant skips a noop-messaging overlay that declares Slack without credentials (#3299).
SLACK = ServiceClient(Service.SLACK, configured_messaging_from_overlay, "Slack messaging backend")
NOTION = ServiceClient(Service.NOTION, notion_client_from_overlay, "Notion client")
SENTRY = ServiceClient(Service.SENTRY, sentry_client_from_overlay, "Sentry org (sentry_org + sentry_token_pass_key)")
SHAREPOINT = ServiceClient(
    Service.SHAREPOINT,
    sharepoint_client_from_overlay,
    "SharePoint document library (TEATREE_SHAREPOINT_* environment)",
)
FORGE_CLIENTS = {GITHUB.service: GITHUB, GITLAB.service: GITLAB}
SERVICE_CLIENTS: dict[Service, Callable[[], object]] = {
    client.service: client.resolve for client in (GITHUB, GITLAB, SLACK, NOTION, SENTRY, SHAREPOINT)
}
