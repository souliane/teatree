"""Registers the Notion write seam the MCP tools consume.

``teatree.cli`` sits ABOVE ``teatree.mcp`` in the layer graph, so the dependency is inverted (same
shape as :mod:`teatree.cli.review.mcp_seam`): :func:`register` installs a factory that builds
:class:`~teatree.cli.notion_write_service.NotionWrites`, and the MCP tools reach it only through
:class:`~teatree.mcp.services_notion.NotionWriteSeam`. ``t3 mcp serve`` calls it, not ``teatree.cli``'s
import: this module loads the MCP SDK, and the startup-budget test bounds what every ``t3`` start imports.
It is also the boundary where every Notion failure becomes a
:class:`~teatree.mcp.services_notion.NotionToolError` carrying the exit code ``t3 notion`` would have exited with.
"""

from typing import TYPE_CHECKING, Any

from teatree.mcp.services_notion import NewNotionPage, NotionComment, NotionToolError, register_notion_write_seam

if TYPE_CHECKING:
    from collections.abc import Callable

    from teatree.cli.notion_write_service import NotionWrites


class _NotionWriter:
    """The seam over one overlay's client: each call builds it fresh, so no write guard or identity is cached."""

    def __init__(self, overlay: str) -> None:
        self._overlay = overlay

    def replace(self, page: str, old_text: str, new_text: str, *, dry_run: bool) -> dict[str, Any]:
        return self._run(lambda writes: writes.replace(page, old_text, new_text, dry_run=dry_run))

    def create(self, page: NewNotionPage, *, dry_run: bool) -> dict[str, Any]:
        return self._run(lambda writes: writes.create(page, dry_run=dry_run))

    def comment(self, comment: NotionComment, *, dry_run: bool) -> dict[str, Any]:
        return self._run(lambda writes: writes.comment(comment, dry_run=dry_run))

    def archive(self, page: str, *, archived: bool, dry_run: bool) -> dict[str, Any]:
        return self._run(lambda writes: writes.archive(page, archived=archived, dry_run=dry_run))

    def property_set(self, page: str, name: str, value: str, *, dry_run: bool) -> dict[str, Any]:
        return self._run(lambda writes: writes.property_set(page, name, value, dry_run=dry_run))

    def discussions(self, page: str, *, verify: bool) -> dict[str, Any]:
        return self._run(lambda writes: writes.discussions(page, verify=verify))

    def _run(self, call: "Callable[[NotionWrites], dict[str, Any]]") -> dict[str, Any]:
        import httpx  # noqa: PLC0415 — deferred: lazy CLI import

        from teatree.backends.notion.errors import NotionError, describe_failure  # noqa: PLC0415 — lazy CLI import
        from teatree.cli.notion_write_service import NotionWrites  # noqa: PLC0415 — deferred: needs the app registry

        try:
            return call(NotionWrites.for_overlay(self._overlay))
        except NotionError as exc:
            raise NotionToolError(exc.exit_code, type(exc).__name__, str(exc)) from exc
        except httpx.HTTPError as exc:
            detail = describe_failure(exc) if isinstance(exc, httpx.HTTPStatusError) else str(exc)
            raise NotionToolError(1, type(exc).__name__, detail) from exc
        except ValueError as exc:
            raise NotionToolError(1, type(exc).__name__, str(exc)) from exc


def register() -> None:
    """Install the Notion write seam factory into :mod:`teatree.mcp.services_notion`."""
    register_notion_write_seam(_NotionWriter)
