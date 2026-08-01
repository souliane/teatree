"""Notion read-only MCP tool group (#3076).

Registered only when a registered overlay declares ``Service.NOTION``. The page
client is resolved through
:func:`teatree.core.backend_factory.notion_client_from_overlay` (a core seam).
Read-only: the status *write* stays on the gated runtime sync, not the MCP
surface.
"""

from asgiref.sync import sync_to_async
from mcp.server.mcpserver import MCPServer
from mcp.types import ToolAnnotations

from teatree.backends.types import Service
from teatree.core.backend_factory import notion_client_from_overlay
from teatree.core.backend_registry import NotionPageClient
from teatree.mcp.service_resolver import resolve_declaring_overlay_client

_READ_ONLY = ToolAnnotations(read_only_hint=True)

INSTRUCTIONS = "- notion_page_status(page_id, property_name): one Notion page's status property value."


def _client() -> NotionPageClient:
    return resolve_declaring_overlay_client(Service.NOTION, notion_client_from_overlay, description="Notion client")


def _live_page_status(page_id: str, property_name: str) -> str | None:
    """The status of a page still proven to be the live version of itself.

    An archived page's ``Status`` reads "In Progress" for as long as it sits in
    the trash, so answering with it is worse than answering nothing: the caller
    gets a confident, current-looking, wrong answer. The tool refuses instead,
    and the refusal names the rule.

    The refusal is a plain ``RuntimeError`` rather than the backend's own
    ``NotionPageNotLiveError``: no module here may import a concrete backend
    (``tests/teatree_mcp/test_transport_boundary.py``), and the boolean on the
    core-owned :class:`~teatree.core.backend_registry.NotionPageClient` seam is
    exactly what that inversion provides for.
    """
    client = _client()
    if not client.page_is_live(page_id):
        msg = (
            f"Notion page {page_id} is archived, in the trash, or could not be proven to be the current "
            "version, so its status is not the current status. An archived page is not a weaker source, it "
            "is not a source at all: ignore it entirely and find the more recent version — "
            "`t3 notion doctor <page>` names it when it can be resolved."
        )
        raise RuntimeError(msg)
    return client.get_page_status(page_id, property_name=property_name)


async def _notion_page_status(page_id: str, *, property_name: str = "Status") -> str | None:
    return await sync_to_async(lambda: _live_page_status(page_id, property_name), thread_sensitive=True)()


def register(server: MCPServer) -> None:
    server.add_tool(_notion_page_status, name="notion_page_status", annotations=_READ_ONLY)
