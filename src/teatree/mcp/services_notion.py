"""Notion read-only MCP tool group (#3076).

Registered only when a registered overlay declares ``Service.NOTION``. The page
client is resolved through
:func:`teatree.core.backend_factory.notion_client_from_overlay` (a core seam).
Read-only: the status *write* stays on the gated runtime sync, not the MCP
surface.
"""

from asgiref.sync import sync_to_async
from mcp.server.fastmcp import FastMCP
from mcp.types import ToolAnnotations

from teatree.backends.types import Service
from teatree.core.backend_factory import notion_client_from_overlay
from teatree.core.backend_registry import NotionPageClient
from teatree.core.overlay_loader import get_all_overlays

_READ_ONLY = ToolAnnotations(readOnlyHint=True)

INSTRUCTIONS = "- notion_page_status(page_id, property_name): one Notion page's status property value."


def _client() -> NotionPageClient:
    for name, overlay in get_all_overlays().items():
        if Service.NOTION in overlay.config.required_third_party_services:
            client = notion_client_from_overlay(name)
            if client is not None:
                return client
    msg = "No registered overlay declares a configured Notion client"
    raise RuntimeError(msg)


async def _notion_page_status(page_id: str, *, property_name: str = "Status") -> str | None:
    return await sync_to_async(
        lambda: _client().get_page_status(page_id, property_name=property_name), thread_sensitive=True
    )()


def register(server: FastMCP) -> None:
    server.add_tool(_notion_page_status, name="notion_page_status", annotations=_READ_ONLY)
