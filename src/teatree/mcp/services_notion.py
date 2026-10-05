"""Notion MCP tool group (#3076): the page-status read, and the discussion read and approval-gated write tools.

Registered only when a registered overlay declares ``Service.NOTION``. The status read resolves its client
through :func:`teatree.core.backend_factory.notion_client_from_overlay` (a core seam); the status *write*
stays on the gated runtime sync. The discussion read and the writes reach the backend only through
:class:`NotionWriteSeam`, and a write is only real once the owner has recorded an approval.

The backend and the approval gate live in ``teatree.cli`` — ABOVE ``teatree.mcp`` in the layer graph, and
``teatree.backends.notion`` is a transport no MCP module may import — so, like :mod:`teatree.mcp.review_seam`,
the dependency is INVERTED: ``t3 mcp serve`` registers a factory via :func:`register_notion_write_seam`. The
default factory raises loud, so a caller that never registered one fails with a clear message rather than
silently bypassing the approval gate.
"""

from collections.abc import Callable
from dataclasses import dataclass
from typing import Any, Protocol

from asgiref.sync import sync_to_async
from mcp.server.mcpserver import MCPServer
from mcp.server.mcpserver.exceptions import ToolError
from mcp.types import ToolAnnotations

from teatree.backends.types import Service
from teatree.core.backend_factory import notion_client_from_overlay
from teatree.core.backend_registry import NotionPageClient
from teatree.mcp.service_resolver import resolve_declaring_overlay_client
from teatree.utils.expanded_params import expand_dataclass_params


class NotionToolError(ToolError):
    """A Notion failure with the exit code ``t3 notion`` would have exited with, in the text the caller reads.

    Only a ``ToolError`` sets ``isError`` on the tool result, so a caller cannot read a failed write as a success.
    """

    def __init__(self, exit_code: int, condition: str, message: str) -> None:
        self.exit_code = exit_code
        super().__init__(f"notion exit {exit_code} ({condition}): {message}")


@dataclass(frozen=True, slots=True)
class NewNotionPage:
    """What ``notion_create`` was asked: the parent, the title, the Markdown body and an optional emoji icon."""

    parent: str
    title: str
    body: str
    icon: str = ""


@dataclass(frozen=True, slots=True)
class NotionComment:
    """What ``notion_comment`` was asked: where the comment goes, and its text and dedup key."""

    page: str
    body: str
    quote: str = ""
    discussion: str = ""
    marker: str = ""


class NotionWriteSeam(Protocol):
    def replace(self, page: str, old_text: str, new_text: str, *, dry_run: bool) -> dict[str, Any]: ...

    def create(self, page: NewNotionPage, *, dry_run: bool) -> dict[str, Any]: ...

    def comment(self, comment: NotionComment, *, dry_run: bool) -> dict[str, Any]: ...

    def archive(self, page: str, *, archived: bool, dry_run: bool) -> dict[str, Any]: ...

    def property_set(self, page: str, name: str, value: str, *, dry_run: bool) -> dict[str, Any]: ...

    def discussions(self, page: str, *, verify: bool) -> dict[str, Any]: ...


SeamFactory = Callable[[str], NotionWriteSeam]


def _unregistered_factory(_overlay: str) -> NotionWriteSeam:
    msg = "Notion write seam not registered — `t3 mcp serve` must call register_notion_write_seam() before serving"
    raise RuntimeError(msg)


@dataclass(slots=True)
class _FactoryRegistry:
    factory: SeamFactory = _unregistered_factory


_factory_registry = _FactoryRegistry()


def register_notion_write_seam(factory: SeamFactory) -> None:
    """Inject the approval-gated write service factory (called by ``t3 mcp serve`` before it builds the server)."""
    _factory_registry.factory = factory


def notion_write_seam(overlay: str) -> NotionWriteSeam:
    """The write service for *overlay* (blank: the overlay the venue's Notion routing singles out)."""
    return _factory_registry.factory(overlay)


_READ_ONLY = ToolAnnotations(read_only_hint=True)
_WRITE = ToolAnnotations(read_only_hint=False, destructive_hint=False)
_DESTRUCTIVE = ToolAnnotations(read_only_hint=False, destructive_hint=True)

INSTRUCTIONS = (
    "- notion_page_status(page_id, property_name): one Notion page's status property value.\n"
    "- notion_discussions(page, verify, overlay): every open discussion under a page or block; a part of "
    "the page that could not be read is an error naming it, never a shorter list.\n"
    "- notion_replace(page, old_text, new_text, dry_run, overlay): replace text that occurs exactly once on "
    "an internal page, inside one block, keeping its formatting. dry_run=true (the default) returns the "
    "block and the diff and writes nothing. A real write needs an approval the OWNER recorded for exactly "
    "that diff (the dry run returns the command); with none it exits 23, and with one recorded for other "
    "text, 20.\n"
    "- notion_create(parent, title, body, icon, dry_run, overlay): create a child page under a page or "
    "database the integration reaches; same dry run and recorded-approval rule; a page already carrying "
    "the title is reported as existing.\n"
    "- notion_comment(page, body, quote, discussion, marker, dry_run, overlay): post ONE comment on the "
    "page, on the block holding `quote`, or as a reply in `discussion`; a marker already there is a "
    "duplicate; same dry run and recorded-approval rule.\n"
    "- notion_archive(page, archived, dry_run, overlay): move a page or database row to the Notion trash "
    "(archived=true, the default) or restore it (archived=false); same dry run and recorded-approval rule, "
    "the approval covering exactly that page, that direction and that overlay.\n"
    "- notion_property_set(page, name, value, dry_run, overlay): set one property of a page or row, typed by "
    "that property's own type, with the same dry run and recorded-approval rule; a type with no plain-text "
    "form is refused.\n"
    "Every Notion failure is an error reading `notion exit <N> (<Class>): <message>`, N being the exit "
    "code `t3 notion` would exit with."
)


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
        raise ToolError(msg)
    return client.get_page_status(page_id, property_name=property_name)


async def _notion_page_status(page_id: str, *, property_name: str = "Status") -> str | None:
    return await sync_to_async(lambda: _live_page_status(page_id, property_name), thread_sensitive=True)()


async def _notion_discussions(page: str, *, verify: bool = False, overlay: str = "") -> dict[str, Any]:
    """Every open discussion anchored anywhere under a page or block, or an error naming what could not be read."""
    return await sync_to_async(
        lambda: notion_write_seam(overlay).discussions(page, verify=verify), thread_sensitive=True
    )()


async def _notion_replace(
    page: str, old_text: str, new_text: str, *, dry_run: bool = True, overlay: str = ""
) -> dict[str, Any]:
    """Replace text occurring exactly once on an internal page, inside one block, keeping its formatting.

    dry_run=true (the default) writes nothing and returns the diff with the approval the owner must record.
    """
    return await sync_to_async(
        lambda: notion_write_seam(overlay).replace(page, old_text, new_text, dry_run=dry_run), thread_sensitive=True
    )()


@expand_dataclass_params
async def _notion_create(page: NewNotionPage, *, dry_run: bool = True, overlay: str = "") -> dict[str, Any]:
    """Create a child page (Markdown body) under a page or database the integration reaches.

    dry_run=true (the default) writes nothing and returns what would be created with the approval to record.
    """
    return await sync_to_async(
        lambda: notion_write_seam(overlay).create(page, dry_run=dry_run), thread_sensitive=True
    )()


@expand_dataclass_params
async def _notion_comment(comment: NotionComment, *, dry_run: bool = True, overlay: str = "") -> dict[str, Any]:
    """Post one comment on the page, on the one block holding `quote`, or as a reply in `discussion`.

    A marker already in the destination is a duplicate and posts nothing. dry_run=true (the default) writes
    nothing and returns what would be posted with the approval to record.
    """
    return await sync_to_async(
        lambda: notion_write_seam(overlay).comment(comment, dry_run=dry_run), thread_sensitive=True
    )()


async def _notion_archive(
    page: str, *, archived: bool = True, dry_run: bool = True, overlay: str = ""
) -> dict[str, Any]:
    """Move a page or database row to the Notion trash (archived=true), or restore it (archived=false).

    dry_run=true (the default) writes nothing and returns the page, what would change and the approval to record.
    """
    return await sync_to_async(
        lambda: notion_write_seam(overlay).archive(page, archived=archived, dry_run=dry_run), thread_sensitive=True
    )()


async def _notion_property_set(
    page: str, name: str, value: str, *, dry_run: bool = True, overlay: str = ""
) -> dict[str, Any]:
    """Set one property of a page or database row to a literal, shaped by the property's own type.

    dry_run=true (the default) writes nothing and returns the old and new value with the approval to record.
    """
    return await sync_to_async(
        lambda: notion_write_seam(overlay).property_set(page, name, value, dry_run=dry_run), thread_sensitive=True
    )()


def register(server: MCPServer) -> None:
    server.add_tool(_notion_page_status, name="notion_page_status", annotations=_READ_ONLY)
    server.add_tool(_notion_discussions, name="notion_discussions", annotations=_READ_ONLY)
    server.add_tool(_notion_replace, name="notion_replace", annotations=_DESTRUCTIVE)
    server.add_tool(_notion_create, name="notion_create", annotations=_WRITE)
    server.add_tool(_notion_comment, name="notion_comment", annotations=_WRITE)
    server.add_tool(_notion_archive, name="notion_archive", annotations=_DESTRUCTIVE)
    server.add_tool(_notion_property_set, name="notion_property_set", annotations=_DESTRUCTIVE)
