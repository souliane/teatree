"""Post ONE comment — on a page, on one block, or into a discussion — refusing to post the same marker twice.

``t3 notion comments`` reads. This is the write half the dedup-driven skills
need: ``/bdd-test-creation`` and ``/prd-agent`` each post a notification comment
and check for their own marker first, so a surface that can only read lets a
headless run perform the dedup CHECK and then strands it at the post.

**Idempotency is the default, not a flag.** The dedup key is the marker the
caller names, found anywhere in a comment — falling back to the comment's whole
text, compared normalised, never as a substring — and a key already present in
the anchor's open discussions (a reply: in its own discussion only) returns
``duplicate`` having written nothing. A caller that genuinely wants a second identical comment asks for it
with ``allow_duplicate``. The safe behaviour is therefore what you get by
forgetting, which is the opposite of a check you must remember to request.

The dedup reads Notion's *unresolved* discussions, which is all the comments
endpoint exposes. A marker whose discussion a human resolved is invisible here
and will be posted again — the honest reading of "resolved": that thread was
handled, so the next run's notification is new information rather than a
duplicate.

**Verification is not optional.** Notion answers ``200`` on the create, so the
poster re-reads the page's discussions and refuses to report success unless the
comment it just created is actually there.
"""

import dataclasses
from typing import cast

from teatree.backends.notion.blocks import literal_rich_text
from teatree.backends.notion.client import NotionClient
from teatree.backends.notion.errors import NotionWriteNotLandedError
from teatree.backends.notion.markdown import rich_text_plain
from teatree.types import RawAPIDict


@dataclasses.dataclass(frozen=True)
class CommentPostResult:
    """What a post did, in the vocabulary a dedup-driven caller branches on."""

    outcome: str
    comment_id: str
    discussion_id: str
    marker: str


@dataclasses.dataclass(frozen=True, slots=True)
class _Destination:
    """Where a comment goes: the object it is anchored on, the API placement, and its discussion when a reply."""

    anchor_id: str
    where: RawAPIDict
    discussion_id: str = ""


class CommentPoster:
    """Post a page comment once per marker, verifying that it landed.

    The dedup is a read of the page's open discussions taken before the create,
    which is the strongest guard the API offers: Notion has neither a
    create-if-absent nor a comment delete, so two writers racing the same marker
    each land a copy.
    """

    def __init__(self, client: NotionClient) -> None:
        self._client = client

    def post(
        self,
        page_id: str,
        body: str,
        *,
        marker: str = "",
        allow_duplicate: bool = False,
        dry_run: bool = False,
    ) -> CommentPostResult:
        destination = _Destination(page_id, {"parent": {"page_id": page_id}})
        return self._post(destination, body, marker=marker, allow_duplicate=allow_duplicate, dry_run=dry_run)

    def post_on_block(self, block_id: str, body: str, *, marker: str = "", dry_run: bool = False) -> CommentPostResult:
        destination = _Destination(block_id, {"parent": {"block_id": block_id}})
        return self._post(destination, body, marker=marker, dry_run=dry_run)

    def reply(
        self, anchor_id: str, discussion_id: str, body: str, *, marker: str = "", dry_run: bool = False
    ) -> CommentPostResult:
        destination = _Destination(anchor_id, {"discussion_id": discussion_id}, discussion_id)
        return self._post(destination, body, marker=marker, dry_run=dry_run)

    def _post(
        self,
        destination: _Destination,
        body: str,
        *,
        marker: str,
        allow_duplicate: bool = False,
        dry_run: bool = False,
    ) -> CommentPostResult:
        text = body.strip()
        if not text:
            msg = "refusing to post an empty comment"
            raise ValueError(msg)
        key = marker or text
        if not allow_duplicate:
            existing = self._matching(destination, marker=marker, text=text)
            if existing is not None:
                return CommentPostResult(
                    outcome="duplicate",
                    comment_id=str(existing.get("id", "")),
                    discussion_id=str(existing.get("discussion_id", "")),
                    marker=key,
                )
        if dry_run:
            self._client.check_writable(destination.anchor_id)
            return CommentPostResult("would post", "", destination.discussion_id, key)
        created = self._client.post_comment(destination.anchor_id, destination.where, literal_rich_text(text))
        return self._verified(destination.anchor_id, created, key)

    def _matching(self, destination: _Destination, *, marker: str, text: str) -> RawAPIDict | None:
        """An explicit marker is a token callers embed in a longer body; without one the whole comment is the key."""
        comments = [
            item
            for item in self._client.list_comments(destination.anchor_id)
            if not destination.discussion_id or _same_id(str(item.get("discussion_id", "")), destination.discussion_id)
        ]
        if marker:
            return next((item for item in comments if marker in comment_text(item)), None)
        wanted = _normalised(text)
        return next((item for item in comments if _normalised(comment_text(item)) == wanted), None)

    def _verified(self, anchor_id: str, created: RawAPIDict, key: str) -> CommentPostResult:
        comment_id = str(created.get("id", ""))
        landed = [item for item in self._client.list_comments(anchor_id) if str(item.get("id", "")) == comment_id]
        if not landed:
            msg = (
                f"the comment reported success but {anchor_id} does not carry comment "
                f"{comment_id!r} on re-fetch — treat the write as failed."
            )
            raise NotionWriteNotLandedError(msg)
        return CommentPostResult(
            outcome="posted",
            comment_id=comment_id,
            discussion_id=str(created.get("discussion_id", "")),
            marker=key,
        )


def _normalised(text: str) -> str:
    return " ".join(text.split()).casefold()


def _same_id(left: str, right: str) -> bool:
    return left.replace("-", "").lower() == right.replace("-", "").lower()


def comment_text(comment: RawAPIDict) -> str:
    """The bare text of a comment, for matching a marker against it."""
    spans = comment.get("rich_text")
    return rich_text_plain(cast("list[RawAPIDict]", spans)) if isinstance(spans, list) else ""
