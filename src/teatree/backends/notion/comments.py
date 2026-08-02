"""Post ONE comment on a page, refusing to post the same marker twice.

``t3 notion comments`` reads. This is the write half the dedup-driven skills
need: ``/bdd-test-creation`` and ``/prd-agent`` each post a notification comment
and check for their own marker first, so a surface that can only read lets a
headless run perform the dedup CHECK and then strands it at the post.

**Idempotency is the default, not a flag.** The dedup key is the marker the
caller names — falling back to the comment's own text — and a key already
present in the page's open discussions returns ``duplicate`` having written
nothing. A caller that genuinely wants a second identical comment asks for it
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


class CommentPoster:
    """Post a page comment at most once per marker, verifying that it landed."""

    def __init__(self, client: NotionClient) -> None:
        self._client = client

    def post(
        self,
        page_id: str,
        body: str,
        *,
        marker: str = "",
        allow_duplicate: bool = False,
    ) -> CommentPostResult:
        text = body.strip()
        if not text:
            msg = "refusing to post an empty comment"
            raise ValueError(msg)
        key = marker or text
        if not allow_duplicate:
            existing = self._matching(page_id, key)
            if existing is not None:
                return CommentPostResult(
                    outcome="duplicate",
                    comment_id=str(existing.get("id", "")),
                    discussion_id=str(existing.get("discussion_id", "")),
                    marker=key,
                )
        created = self._client.create_comment(page_id, literal_rich_text(text))
        return self._verified(page_id, created, key)

    def _matching(self, page_id: str, key: str) -> RawAPIDict | None:
        return next((item for item in self._client.list_comments(page_id) if key in comment_text(item)), None)

    def _verified(self, page_id: str, created: RawAPIDict, key: str) -> CommentPostResult:
        comment_id = str(created.get("id", ""))
        landed = [item for item in self._client.list_comments(page_id) if str(item.get("id", "")) == comment_id]
        if not landed:
            msg = (
                f"the comment reported success but page {page_id} does not carry comment "
                f"{comment_id!r} on re-fetch — treat the write as failed."
            )
            raise NotionWriteNotLandedError(msg)
        return CommentPostResult(
            outcome="posted",
            comment_id=comment_id,
            discussion_id=str(created.get("discussion_id", "")),
            marker=key,
        )


def comment_text(comment: RawAPIDict) -> str:
    """The bare text of a comment, for matching a marker against it."""
    spans = comment.get("rich_text")
    return rich_text_plain(cast("list[RawAPIDict]", spans)) if isinstance(spans, list) else ""
