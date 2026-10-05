"""The Notion writes behind the MCP tools: probe, recorded approval, write, verify, audit.

Print-free and typer-free, because over stdio the tool's stdout is the JSON-RPC channel. Every
operation but archive is the CLI's own sequence; the write goes through the same ``NotionClient._write``
guard, and the one thing the CLI does not do is spend a recorded approval (``t3 notion`` stays ungated).
``apply_replace`` is shared with ``t3 notion replace``.
"""

import dataclasses
from typing import Literal

from teatree.backends.notion.archive import PageArchiver
from teatree.backends.notion.blocks import build_blocks
from teatree.backends.notion.client import NotionClient
from teatree.backends.notion.comments import CommentPoster, CommentPostResult
from teatree.backends.notion.discussions import DiscussionEnumerator
from teatree.backends.notion.errors import NotionDiscussionNotFoundError, NotionPageNotLiveError
from teatree.backends.notion.liveness import Liveness, LivenessVerdict, title_of
from teatree.backends.notion.pages import PageCreator
from teatree.backends.notion.properties import (
    PagePropertyWriter,
    build_property_write,
    page_property,
    plain_property_value,
    property_type,
)
from teatree.backends.notion.replace import PageText
from teatree.cli.notion_replace import apply_replace, replace_preview, replace_result
from teatree.cli.notion_support import live_page, notion_client, object_id, writer_identity
from teatree.cli.notion_write_approval import approval_hint, spent_approval, write_target
from teatree.mcp.services_notion import NewNotionPage, NotionComment
from teatree.types import RawAPIDict

_REPLACE, _CREATE, _COMMENT = "notion_replace", "notion_create", "notion_comment"
_ARCHIVE, _RESTORE, _PROPERTY = "notion_archive", "notion_restore", "notion_property_set"


@dataclasses.dataclass(frozen=True, slots=True)
class _CommentTarget:
    anchor_id: str
    text: str
    placement: Literal["page", "block", "reply"]
    discussion_id: str = ""

    @property
    def object_path(self) -> str:
        return f"{self.anchor_id}/{self.discussion_id}" if self.discussion_id else self.anchor_id

    def send(self, poster: CommentPoster, *, marker: str, dry_run: bool) -> CommentPostResult:
        if self.placement == "reply":
            return poster.reply(self.anchor_id, self.discussion_id, self.text, marker=marker, dry_run=dry_run)
        if self.placement == "block":
            return poster.post_on_block(self.anchor_id, self.text, marker=marker, dry_run=dry_run)
        return poster.post(self.anchor_id, self.text, marker=marker, dry_run=dry_run)


class NotionWrites:
    """The six MCP operations over one client: a dry run asks nothing, a real write spends a recorded approval."""

    def __init__(self, client: NotionClient) -> None:
        self._client = client

    @classmethod
    def for_overlay(cls, overlay: str) -> "NotionWrites":
        return cls(notion_client(overlay))

    def replace(self, page: str, old: str, new: str, *, dry_run: bool) -> RawAPIDict:
        if not old:
            msg = "old_text is empty; there is nothing to anchor the replace on."
            raise ValueError(msg)
        if old == new:
            msg = "old_text and new_text hold the same text; there is nothing to replace."
            raise ValueError(msg)
        replacer = PageText(self._client)
        plan = replacer.plan(live_page(self._client, page).page_id, old=old, new=new)
        self._client.check_writable(plan.slot.block_id)
        if plan.already_applied:
            return replace_result(plan, "already applied", plan.expected_counts)
        identity = writer_identity(self._client)
        target = self._target(
            f"{plan.page_id}#{plan.slot.block_id}" + ("" if plan.slot.cell is None else f".{plan.slot.cell}"),
            plan.page_id,
            plan.slot.block_id,
            plan.slot.cell,
            plan.before,
            plan.old,
            plan.new,
        )
        if dry_run:
            return {
                "outcome": "dry_run",
                "already_applied": False,
                **replace_preview(plan),
                **self._writing_as(identity),
                "approval": approval_hint(target, _REPLACE),
            }
        replacer.ensure_unchanged(plan)
        with spent_approval(target, _REPLACE):
            counts = apply_replace(replacer, plan, identity=identity)
        return {**replace_result(plan, "replaced", counts), "verified": True, **self._writing_as(identity)}

    def create(self, request: NewNotionPage, *, dry_run: bool) -> RawAPIDict:
        name, body = request.title.strip(), request.body
        if not name:
            msg = "title must name the page; a blank title is refused."
            raise ValueError(msg)
        if not body.strip():
            msg = "body is empty; a page is created with its body."
            raise ValueError(msg)
        parent_id = object_id(request.parent)
        blocks = build_blocks(body)
        creator = PageCreator(self._client)
        probe = creator.dry_run(parent_id, title=name, blocks=blocks, markdown=body)
        existing: RawAPIDict = {"page_id": probe.page_id, "url": probe.url}
        preview: RawAPIDict = {"outcome": "dry_run", "parent_id": parent_id, "title": name}
        if probe.outcome == "exists":
            return {**preview, "would": "exists", **existing} if dry_run else {"outcome": "exists", **existing}
        identity = writer_identity(self._client)
        target = self._target(parent_id, name, request.icon, body)
        if dry_run:
            return {
                **preview,
                "would": "create",
                "blocks": len(blocks),
                **self._writing_as(identity),
                "approval": approval_hint(target, _CREATE),
            }
        with spent_approval(target, _CREATE):
            created = creator.create(parent_id, title=name, blocks=blocks, icon=request.icon, markdown=body)
        return {
            "outcome": created.outcome,
            "page_id": created.page_id,
            "url": created.url,
            "blocks": created.blocks,
            "verified": created.outcome == "created",
            **self._writing_as(identity),
        }

    def comment(self, request: NotionComment, *, dry_run: bool) -> RawAPIDict:
        if request.quote and request.discussion:
            msg = "pass quote or discussion, not both: a comment opens on the quoted block or replies in a discussion."
            raise ValueError(msg)
        if not request.body.strip():
            msg = "body is empty; there is nothing to post."
            raise ValueError(msg)
        destination = self._comment_target(live_page(self._client, request.page).page_id, request)
        poster = CommentPoster(self._client)
        probe = destination.send(poster, marker=request.marker, dry_run=True)
        found: RawAPIDict = {
            "anchor_id": destination.anchor_id,
            "discussion_id": probe.discussion_id,
            "marker": probe.marker,
        }
        if probe.outcome == "duplicate":
            if dry_run:
                return {"outcome": "dry_run", "would": "duplicate", **found}
            return {"outcome": "duplicate", "comment_id": probe.comment_id, **found}
        identity = writer_identity(self._client)
        target = self._target(destination.object_path, destination.text, request.marker)
        if dry_run:
            return {
                "outcome": "dry_run",
                "would": "post",
                **found,
                **self._writing_as(identity),
                "approval": approval_hint(target, _COMMENT),
            }
        with spent_approval(target, _COMMENT):
            posted = destination.send(poster, marker=request.marker, dry_run=False)
        return {
            "outcome": posted.outcome,
            "comment_id": posted.comment_id,
            **found,
            "discussion_id": posted.discussion_id,
            "verified": posted.outcome == "posted",
            **self._writing_as(identity),
        }

    def archive(self, page: str, *, archived: bool, dry_run: bool) -> RawAPIDict:
        page_id = object_id(page)
        if archived:
            self._client.check_writable(page_id, archived=True)
        verdict = self._client.page_liveness(page_id)
        if verdict.state is Liveness.UNKNOWN:
            raise self._unprovable(page_id, verdict, archived=archived)
        self._client.check_writable(page_id)
        page_data = self._client.get_page(page_id)
        found = {"page_id": page_id, "title": title_of(page_data)[1], "url": str(page_data.get("url", ""))}
        if (verdict.state is Liveness.DEAD) == archived:
            return {"outcome": "already applied", **found}
        if not archived and page_data.get("archived") is not True:
            raise self._unprovable(page_id, verdict, archived=False)
        direction = "archive" if archived else "restore"
        child_pages = sum(child.get("type") == "child_page" for child in self._client.list_block_children(page_id))
        identity = writer_identity(self._client)
        target = self._target(page_id, direction)
        if dry_run:
            return {
                "outcome": "dry_run",
                "would": direction,
                **found,
                "child_pages": child_pages,
                **self._writing_as(identity),
                "approval": approval_hint(target, _ARCHIVE if archived else _RESTORE),
            }
        with spent_approval(target, _ARCHIVE if archived else _RESTORE):
            PageArchiver(self._client).write(page_id, archived=archived)
        return {
            "outcome": "archived" if archived else "restored",
            **found,
            "verified": True,
            **self._writing_as(identity),
        }

    def property_set(self, page: str, name: str, value: str, *, dry_run: bool) -> RawAPIDict:
        page_id = live_page(self._client, page).page_id
        before = page_property(self._client.get_page(page_id), name)
        intended = build_property_write(before, value)
        self._client.check_writable(page_id)
        previous = plain_property_value(before)
        found: RawAPIDict = {"page_id": page_id, "property": name, "type": property_type(before)}
        if previous == intended.expected_plain:
            return {"outcome": "already applied", **found, "value": previous}
        identity = writer_identity(self._client)
        target = self._target(page_id, name, value, found["type"], previous)
        if dry_run:
            return {
                "outcome": "dry_run",
                "would": "set",
                **found,
                "previous": previous,
                "value": intended.expected_plain,
                **self._writing_as(identity),
                "approval": approval_hint(target, _PROPERTY),
            }
        with spent_approval(target, _PROPERTY):
            written = PagePropertyWriter(self._client).write(
                page_id, name=name, value=value, expected_type=found["type"], expected_previous=previous
            )
        return {
            "outcome": "set",
            **found,
            "previous": written.previous,
            "value": written.value,
            "verified": True,
            **self._writing_as(identity),
        }

    def discussions(self, page: str, *, verify: bool) -> RawAPIDict:
        found = DiscussionEnumerator(self._client).enumerate(object_id(page), cross_check=verify)
        found.raise_if_incomplete()
        return found.as_dict()

    def _comment_target(self, page_id: str, request: NotionComment) -> _CommentTarget:
        text = request.body.strip()
        if request.quote:
            slot, _start, _slots = PageText(self._client).locate(page_id, request.quote)
            return _CommentTarget(slot.block_id, f"“{request.quote}”\n\n{text}", "block")
        if request.discussion:
            found = DiscussionEnumerator(self._client).enumerate(page_id)
            anchor = found.anchor_of(request.discussion)
            if anchor is None:
                found.raise_if_incomplete()
                msg = (
                    f"discussion {request.discussion} is not an open discussion on page {page_id}. Nothing was posted."
                )
                raise NotionDiscussionNotFoundError(msg)
            return _CommentTarget(anchor, text, "reply", request.discussion)
        return _CommentTarget(page_id, text, "page")

    @staticmethod
    def _unprovable(page_id: str, verdict: LivenessVerdict, *, archived: bool) -> NotionPageNotLiveError:
        if archived:
            return verdict.as_error(page_id)
        return NotionPageNotLiveError(
            f"refusing to restore {page_id}: Notion does not say it is archived (liveness: {verdict.state.value} "
            f"[{verdict.reason}]), so there is nothing this tool may undo. Nothing was written."
        )

    def _target(self, object_path: str, *bound: str | int | None) -> str:
        """The approval scope; the overlay the write runs as is bound, so an approval is not spendable under another."""
        return write_target(object_path, self._client.overlay or "", *bound)

    def _writing_as(self, identity: RawAPIDict) -> RawAPIDict:
        return {
            "writing_as": {
                "integration": identity.get("name"),
                "bot_id": identity.get("id"),
                "overlay": self._client.overlay or "",
            }
        }
