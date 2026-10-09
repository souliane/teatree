"""Create a child page under a parent the integration already reaches, and verify body writes by re-read.

Notion's API does not let an internal integration create a workspace-level page, so the parent — a
page or a database — is always given, and it must be shared with the integration. The create goes
through the client's write guard like every other mutation, and at most one page per title is
created under a parent: a second run reports ``exists`` having written nothing.
"""

import dataclasses
from typing import cast

from teatree.backends.notion.blocks import literal_rich_text
from teatree.backends.notion.client import APPEND_BATCH_SIZE, NotionClient
from teatree.backends.notion.errors import (
    NotionError,
    NotionNotSharedError,
    NotionPageNotLiveError,
    NotionWriteNotLandedError,
)
from teatree.backends.notion.liveness import title_of
from teatree.backends.notion.markdown import BlockMarkdownRenderer
from teatree.types import RawAPIDict

WORKSPACE_LEVEL_LIMIT = (
    "Notion's API does not let an internal integration create a workspace-level (top-level private) page, "
    "so a page can only be created under a parent the integration already reaches: "
    "share the parent with the integration named by `t3 notion whoami` (••• → Connections)."
)

#: A probe shorter than this is punctuation or a list marker, not identifying text.
_MIN_PROBE_LENGTH = 3


@dataclasses.dataclass(frozen=True, slots=True)
class CreatedPage:
    outcome: str
    page_id: str
    url: str
    blocks: int


@dataclasses.dataclass(frozen=True, slots=True)
class _Parent:
    link: RawAPIDict
    title_property: str


class PageCreator:
    def __init__(self, client: NotionClient) -> None:
        self._client = client

    def create(self, parent_id: str, *, title: str, blocks: list[RawAPIDict], icon: str, markdown: str) -> CreatedPage:
        parent = self._parent(parent_id)
        existing = self._titled(parent_id, parent, title)
        if existing:
            return self._existing(existing, title=title, blocks=blocks, markdown=markdown)
        created = self._client.create_page(
            parent_id,
            parent=parent.link,
            properties={parent.title_property: {"title": literal_rich_text(title)}},
            children=blocks[:APPEND_BATCH_SIZE],
            icon=icon,
        )
        page_id = str(created.get("id", ""))
        if rest := blocks[APPEND_BATCH_SIZE:]:
            self._client.append_block_children(page_id, rest)
        self._verify_title(page_id, title)
        verify_landed(self._client, page_id, markdown=markdown, expected_blocks=len(blocks))
        return CreatedPage("created", page_id, str(created.get("url", "")), len(blocks))

    def dry_run(self, parent_id: str, *, title: str, blocks: list[RawAPIDict], markdown: str) -> CreatedPage:
        """What :meth:`create` would do — ``exists`` or ``would create`` — having written nothing."""
        parent = self._parent(parent_id)
        existing = self._titled(parent_id, parent, title)
        if existing:
            return self._existing(existing, title=title, blocks=blocks, markdown=markdown)
        self._client.check_writable(parent_id)
        return CreatedPage("would create", "", "", len(blocks))

    def _parent(self, parent_id: str) -> _Parent:
        try:
            block = self._client.get_block(parent_id)
        except NotionNotSharedError as exc:
            msg = f"{exc}\n{WORKSPACE_LEVEL_LIMIT}"
            raise NotionNotSharedError(msg) from exc
        kind = block.get("type")
        if kind == "child_page":
            verdict = self._client.page_liveness(parent_id)
            if not verdict.readable:
                raise verdict.as_error(parent_id)
            return _Parent({"page_id": parent_id}, "title")
        if kind == "child_database":
            if block.get("archived") or block.get("in_trash"):
                msg = f"database {parent_id} is archived or in the trash; create under a live parent instead."
                raise NotionPageNotLiveError(msg)
            return _Parent({"database_id": parent_id}, title_of(self._client.get_database(parent_id))[0] or "title")
        msg = f"{parent_id} is a {kind!r} block; a page can only be created under a page or a database."
        raise NotionError(msg)

    def _titled(self, parent_id: str, parent: _Parent, title: str) -> str:
        if "database_id" in parent.link:
            rows = self._client.query_database(
                parent_id, db_filter={"property": parent.title_property, "title": {"equals": title}}
            )
            return next((str(row.get("id", "")) for row in rows if title_of(row)[1] == title), "")
        return next(
            (
                str(child.get("id", ""))
                for child in self._client.list_block_children(parent_id)
                if child.get("type") == "child_page"
                and cast("RawAPIDict", child.get("child_page") or {}).get("title") == title
            ),
            "",
        )

    def _existing(self, page_id: str, *, title: str, blocks: list[RawAPIDict], markdown: str) -> CreatedPage:
        """A same-title page counts as this create only when it carries the whole body; a shorter one is reported."""
        children = self._client.list_block_children(page_id)
        probe = landing_probe(markdown)
        rendered = BlockMarkdownRenderer(self._client.list_block_children).render(children) if probe else ""
        if len(children) < len(blocks) or probe not in rendered:
            msg = (
                f"page {page_id} under the parent already carries the title {title!r} but holds {len(children)} of "
                f"{len(blocks)} block(s) this body asks for, or not its last line. If it is an earlier create "
                f"of this page, append the missing blocks with `t3 notion append {page_id} --body-file …`; "
                "otherwise pick another title or delete it and re-run `t3 notion create`. Nothing was written."
            )
            raise NotionWriteNotLandedError(msg)
        return CreatedPage("exists", page_id, str(self._client.get_page(page_id).get("url", "")), 0)

    def _verify_title(self, page_id: str, title: str) -> None:
        landed = title_of(self._client.get_page(page_id))[1]
        if landed != title:
            msg = (
                f"the create reported success but page {page_id} is titled {landed!r}, not {title!r}, on re-fetch — "
                "treat the write as failed."
            )
            raise NotionWriteNotLandedError(msg)


def landing_probe(markdown: str) -> str:
    """The text the re-fetch must contain for a Markdown write to count as landed."""
    candidates = [
        line.strip().lstrip("#>-* ") for line in markdown.splitlines() if len(line.strip()) > _MIN_PROBE_LENGTH
    ]
    return candidates[-1] if candidates else ""


def verify_landed(client: NotionClient, page_id: str, *, markdown: str, expected_blocks: int, before: int = 0) -> None:
    """Re-fetch *page_id* and raise unless the body a write reported is on it.

    Raw blocks, or Markdown with no line long enough to probe for, carry no text anchor —
    the child count is then the only evidence the write landed.
    """
    children = client.list_block_children(page_id)
    if probe := landing_probe(markdown):
        if probe not in BlockMarkdownRenderer(client.list_block_children).render(children):
            msg = (
                f"the write reported success but page {page_id} does not contain {probe!r} on re-fetch — "
                "treat the write as failed."
            )
            raise NotionWriteNotLandedError(msg)
    elif len(children) - before < expected_blocks:
        msg = (
            f"the write reported success but page {page_id} gained {len(children) - before} of "
            f"{expected_blocks} block(s) on re-fetch — treat the write as failed."
        )
        raise NotionWriteNotLandedError(msg)
