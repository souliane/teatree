"""Read-only SharePoint/OneDrive document-library MCP tool group (#3084).

Registered by :func:`teatree.mcp.server.build_server` only when a registered
overlay declares ``Service.SHAREPOINT`` in ``required_third_party_services``.
The client is built through
:func:`teatree.core.backend_factory.sharepoint_client_from_overlay` (a core
seam), never a direct ``teatree.backends.sharepoint`` import, so the
transport-boundary fitness test holds. Remote / root / encrypted-config path /
password-command come from the ``TEATREE_SHAREPOINT_*`` wrapper environment (set
by the private skill), keeping tenant/site specifics out of this repo.

Every tool is read-only: it lists, streams, or verifies a live document library
and its share links, and never writes to the remote — read-only is guaranteed at
the OAuth-scope level (see :mod:`teatree.backends.sharepoint`).
"""

from typing import TYPE_CHECKING

from asgiref.sync import sync_to_async
from mcp.server.mcpserver import MCPServer
from mcp.types import ToolAnnotations

from teatree.mcp.service_resolver import SHAREPOINT
from teatree.types import ShareLinkVerification, SharePointEntry

if TYPE_CHECKING:
    from teatree.core.backend_registry import SharePointReadClient

_READ_ONLY = ToolAnnotations(read_only_hint=True)


def _client() -> "SharePointReadClient":
    return SHAREPOINT.resolve()


async def _sharepoint_list(subpath: str = "", *, recursive: bool = True) -> list[SharePointEntry]:
    """Entries under *subpath* in the read-only SharePoint library: Path/Name/Size/IsDir/ModTime."""
    return await sync_to_async(lambda: _client().list_files(subpath, recursive=recursive), thread_sensitive=True)()


async def _sharepoint_cat(file_path: str) -> str:
    """Stream one library file's contents from the remote as text."""
    return await sync_to_async(lambda: _client().cat(file_path), thread_sensitive=True)()


async def _sharepoint_verify_link(folder_path: str = "") -> ShareLinkVerification:
    """The stable ``?id=`` deep-link for *folder_path* plus whether it exists live."""
    return await sync_to_async(lambda: _client().verify_link(folder_path), thread_sensitive=True)()


async def _sharepoint_verify_read_only() -> bool:
    """Assert the remote refuses writes (returns ``True``; raises if it accepts one)."""
    return await sync_to_async(lambda: _client().verify_read_only(), thread_sensitive=True)()


def register(server: MCPServer) -> None:
    server.add_tool(_sharepoint_list, name="sharepoint_list", annotations=_READ_ONLY)
    server.add_tool(_sharepoint_cat, name="sharepoint_cat", annotations=_READ_ONLY)
    server.add_tool(_sharepoint_verify_link, name="sharepoint_verify_link", annotations=_READ_ONLY)
    server.add_tool(_sharepoint_verify_read_only, name="sharepoint_verify_read_only", annotations=_READ_ONLY)
