"""Every discussion on a page — page-scoped AND block-anchored — or a loud gap.

``GET /v1/comments?block_id=<page id>`` answers with the threads whose parent is
the PAGE. An inline comment's parent is the block it is anchored to, so a
page-scoped read returns none of them and says nothing about it: measured on one
live specification page, the page-scoped read returned 1 comment where the block
walk returns 14 across 13 discussions. An agent that asked "are there comments
here?" got a well-formed answer that was wrong by twelve threads, and a
requirement was written on the strength of the one it could not see.

So the enumeration walks the block tree — toggles, columns, synced blocks, child
pages, embedded database rows — and reads the comments anchored at each object.

**A result is never just a list.** Every object the walk could not read becomes a
:class:`CoverageGap`, and a result carrying one is INCOMPLETE: it renders under a
refusal, exits non-zero from the CLI, and raises from
:meth:`PageDiscussions.raise_if_incomplete`. A shorter list that reads as the
whole set is the defect; an error is an acceptable outcome.

**And a complete result is still not an exhaustive one.** The public API cannot
see resolved threads, reaction threads, or suggested edits, and publishes no
page-level total to reconcile against. Those four are stated on every result
(:data:`UNPROVABLE_BY_THIS_API`) rather than left for a caller to infer from
silence.
"""

import dataclasses
from typing import cast

from teatree.backends.notion.client import NotionClient
from teatree.backends.notion.errors import NotionError, NotionIncompleteEnumerationError
from teatree.backends.notion.markdown import rich_text_plain
from teatree.types import RawAPIDict

#: Notion's own block nesting is shallow; a deeper walk than this is a cycle the
#: id-seen guard already covers, or a page no reader could hold in their head.
DEFAULT_MAX_DEPTH = 24

#: Every object costs one comments read, so a page this large is capped and the cap is reported.
DEFAULT_MAX_OBJECTS = 1000

_ANCHOR_TEXT_LIMIT = 80

#: What this enumeration structurally cannot see, reported on EVERY result —
#: including a complete one, because "complete" here means "every object the API
#: exposed was read", not "every thread on the page was found".
UNPROVABLE_BY_THIS_API = (
    (
        "resolved threads are absent: GET /v1/comments returns unresolved comments only, "
        "so every comment below is open and a settled one is invisible."
    ),
    (
        "reaction threads are absent: a discussion Notion classifies as a reaction carries "
        "no comment objects and does not appear here at all."
    ),
    "suggested edits are absent: the public API exposes none.",
    (
        "there is no page-level total to reconcile against: Notion publishes no discussion "
        "count for a page, so this result cannot be cross-checked against one."
    ),
)

_BLOCK_TYPE_PAGE = "page"
_BLOCK_TYPE_DATABASE = "child_database"
_BLOCK_TYPE_DATABASE_ROW = "database_row"


@dataclasses.dataclass(frozen=True, slots=True)
class AnchoredComment:
    """One comment, with the anchor a caller needs to cite it and re-resolve it."""

    comment_id: str
    discussion_id: str
    block_id: str
    block_type: str
    author: str
    created_time: str
    text: str
    anchor_text: str = ""

    def as_dict(self) -> RawAPIDict:
        return dataclasses.asdict(self)


@dataclasses.dataclass(frozen=True, slots=True)
class CoverageGap:
    """One object the walk could not read — what makes a result INCOMPLETE."""

    kind: str
    object_id: str
    detail: str

    def as_dict(self) -> RawAPIDict:
        return dataclasses.asdict(self)


@dataclasses.dataclass(frozen=True, slots=True)
class PageDiscussions:
    """What the walk found, and everything it could not see."""

    page_id: str
    comments: tuple[AnchoredComment, ...]
    gaps: tuple[CoverageGap, ...]
    objects_walked: int
    objects_read: int = 0
    page_read: bool = True

    @property
    def complete(self) -> bool:
        return not self.gaps

    @property
    def discussion_ids(self) -> frozenset[str]:
        return frozenset(comment.discussion_id for comment in self.comments)

    def anchor_of(self, discussion_id: str) -> str | None:
        """The object *discussion_id* is anchored on, or ``None`` when it is not one of this page's discussions."""
        wanted = discussion_id.replace("-", "").lower()
        return next(
            (comment.block_id for comment in self.comments if comment.discussion_id.replace("-", "").lower() == wanted),
            None,
        )

    def raise_if_incomplete(self) -> None:
        """Refuse to hand a partial set to a caller that asked for the page's discussions."""
        if self.complete:
            return
        listed = "\n".join(f"  {gap.object_id} not scanned ({gap.kind}): {gap.detail}" for gap in self.gaps)
        msg = (
            f"the enumeration of page {self.page_id} could not read {len(self.gaps)} object(s), so what it "
            f"found is not the page's full discussion set:\n{listed}"
        )
        raise NotionIncompleteEnumerationError(msg)

    def as_dict(self) -> RawAPIDict:
        return {
            "page_id": self.page_id,
            "complete": self.complete,
            "objects_walked": self.objects_walked,
            "objects_read": self.objects_read,
            "discussion_count": len(self.discussion_ids),
            "comment_count": len(self.comments),
            "comments": [comment.as_dict() for comment in self.comments],
            "gaps": [gap.as_dict() for gap in self.gaps],
            "not_covered_by_this_api": list(UNPROVABLE_BY_THIS_API),
        }


class _Walk:
    """One traversal's accumulator — comments keyed by id, gaps in discovery order."""

    def __init__(self) -> None:
        self.comments: dict[str, AnchoredComment] = {}
        self.gaps: list[CoverageGap] = []
        self.seen: set[str] = set()
        self.read: set[str] = set()
        self.capped = False

    def gap(self, kind: str, object_id: str, detail: str) -> None:
        self.gaps.append(CoverageGap(kind=kind, object_id=object_id, detail=detail))


class DiscussionEnumerator:
    """Walk a page's whole block tree and collect every discussion anchored in it."""

    def __init__(
        self, client: NotionClient, *, max_depth: int = DEFAULT_MAX_DEPTH, max_objects: int = DEFAULT_MAX_OBJECTS
    ) -> None:
        self._client = client
        self._max_depth = max_depth
        self._max_objects = max_objects

    def enumerate(self, page_id: str, *, cross_check: bool = False) -> PageDiscussions:
        """Enumerate once, or twice and report any divergence between the two reads.

        ``cross_check`` doubles the API cost, so it is the caller's call rather
        than the default — but it is the only way to catch a thread that is
        served by one read and omitted by the next, which is otherwise
        indistinguishable from a thread that was never there.
        """
        first = self._traverse(page_id)
        return first if not cross_check else self._reconciled(first, self._traverse(page_id))

    def _traverse(self, page_id: str) -> PageDiscussions:
        walk = _Walk()
        self._visit(_Node(page_id, _BLOCK_TYPE_PAGE), walk, depth=0)
        return PageDiscussions(
            page_id=page_id,
            comments=tuple(walk.comments.values()),
            gaps=tuple(walk.gaps),
            objects_walked=len(walk.seen),
            objects_read=len(walk.read),
            page_read=page_id in walk.read,
        )

    def _visit(self, node: "_Node", walk: _Walk, *, depth: int) -> None:
        if node.object_id in walk.seen:
            return
        if len(walk.seen) >= self._max_objects:
            if not walk.capped:
                walk.capped = True
                walk.gap(
                    "object_capped",
                    node.object_id,
                    f"the walk stopped after {self._max_objects} objects; this one and every later one are unread",
                )
            return
        walk.seen.add(node.object_id)
        self._collect_comments(node, walk)
        if not node.has_children and node.block_type != _BLOCK_TYPE_DATABASE:
            return
        if depth >= self._max_depth:
            walk.gap("depth_capped", node.object_id, f"the walk stopped at depth {depth}; anything below is unread")
            return
        if node.block_type == _BLOCK_TYPE_DATABASE:
            self._descend_rows(node.object_id, walk, depth=depth)
            return
        self._descend_children(node.object_id, walk, depth=depth)

    def _collect_comments(self, node: "_Node", walk: _Walk) -> None:
        try:
            found = self._client.list_comments(node.object_id)
        except NotionError as exc:
            walk.gap("comments_unreadable", node.object_id, str(exc))
            return
        walk.read.add(node.object_id)
        for raw in found:
            comment = _anchored_comment(raw, node)
            walk.comments[comment.comment_id] = comment

    def _descend_children(self, object_id: str, walk: _Walk, *, depth: int) -> None:
        try:
            children = self._client.list_block_children(object_id)
        except NotionError as exc:
            walk.gap("children_unreadable", object_id, str(exc))
            return
        for child in children:
            self._visit(_Node.of(child), walk, depth=depth + 1)

    def _descend_rows(self, database_id: str, walk: _Walk, *, depth: int) -> None:
        """An embedded database's rows are pages with their own discussions, reached through its data sources."""
        try:
            sources = self._client.list_data_sources(database_id)
        except NotionError as exc:
            walk.gap("database_unreadable", database_id, str(exc))
            return
        if not sources:
            walk.gap(
                "database_unreadable",
                database_id,
                "this integration is granted none of the database's data sources, so its rows "
                "and their comments are unread — share the database with the integration",
            )
            return
        for source in sources:
            self._descend_source(str(source.get("id", "")), database_id, walk, depth=depth)

    def _descend_source(self, source_id: str, database_id: str, walk: _Walk, *, depth: int) -> None:
        try:
            rows = self._client.query_data_source(source_id, max_rows=max(self._max_objects - len(walk.seen), 0) + 1)
        except NotionError as exc:
            walk.gap("database_unreadable", database_id, str(exc))
            return
        for row in rows:
            self._visit(_Node(str(row.get("id", "")), _BLOCK_TYPE_DATABASE_ROW), walk, depth=depth + 1)

    @staticmethod
    def _reconciled(first: PageDiscussions, second: PageDiscussions) -> PageDiscussions:
        """Union both reads, and make any difference between them a gap.

        The union is deliberate: a comment one read served and the other omitted
        stays in the output, flagged. Dropping it would reproduce the very
        disappearance the cross-check exists to detect.
        """
        merged = {comment.comment_id: comment for comment in (*second.comments, *first.comments)}
        gaps = tuple(dict.fromkeys((*first.gaps, *second.gaps)))
        drifted = sorted(
            {comment.comment_id for comment in first.comments} ^ {comment.comment_id for comment in second.comments}
        )
        if drifted:
            gaps = (
                *gaps,
                CoverageGap(
                    kind="divergent_reread",
                    object_id=first.page_id,
                    detail=(
                        f"two reads of this page disagreed on {len(drifted)} comment(s) "
                        f"({', '.join(drifted)}); they are included above but the set is not reproducible"
                    ),
                ),
            )
        return PageDiscussions(
            page_id=first.page_id,
            comments=tuple(merged.values()),
            gaps=gaps,
            objects_walked=max(first.objects_walked, second.objects_walked),
            objects_read=min(first.objects_read, second.objects_read),
            page_read=first.page_read and second.page_read,
        )


def render_discussions(result: PageDiscussions, *, as_json: bool) -> str:
    """Render the result so a complete one and a partial one can never read alike."""
    if as_json:
        import json  # noqa: PLC0415 — deferred: only the JSON form pays the import

        return json.dumps(result.as_dict(), indent=2)
    tally = (
        f"{len(result.discussion_ids)} discussion(s), {len(result.comments)} comment(s) "
        f"across {result.objects_walked} object(s)."
    )
    lines = [
        f"## Discussions on {result.page_id}",
        "",
        tally,
        "",
        *_coverage_lines(result),
        "",
        "NOT COVERED BY THE PUBLIC API — true even when coverage is COMPLETE:",
        *(f"  - {caveat}" for caveat in UNPROVABLE_BY_THIS_API),
        "",
    ]
    lines.extend(_comment_lines(result))
    return "\n".join(lines)


def _coverage_lines(result: PageDiscussions) -> list[str]:
    below = result.objects_read - (1 if result.page_read else 0)
    scanned = (
        f"page-level + {below} block(s) scanned"
        if result.page_read
        else f"page-level NOT scanned, {below} block(s) scanned"
    )
    if result.complete:
        return [f"COVERAGE: COMPLETE — {scanned}; every object reachable from this page was read."]
    headline = (
        f"COVERAGE: INCOMPLETE — {scanned}, {len(result.gaps)} not scanned. "
        "What follows is NOT the page's full discussion set."
    )
    return [headline, *(f"  {gap.object_id} not scanned ({gap.kind}): {gap.detail}" for gap in result.gaps)]


def _comment_lines(result: PageDiscussions) -> list[str]:
    if not result.comments:
        return ["(no open discussions found)"]
    by_discussion: dict[str, list[AnchoredComment]] = {}
    for comment in sorted(result.comments, key=lambda item: item.created_time):
        by_discussion.setdefault(comment.discussion_id, []).append(comment)
    lines: list[str] = []
    for discussion_id, comments in by_discussion.items():
        anchor = comments[0]
        excerpt = f": “{anchor.anchor_text}”" if anchor.anchor_text else ""
        lines.append(f"### discussion {discussion_id} — on {anchor.block_type} {anchor.block_id}{excerpt}")
        lines.extend(f"  {item.author} @ {item.created_time}: {item.text}" for item in comments)
        lines.append("")
    return lines


@dataclasses.dataclass(frozen=True, slots=True)
class _Node:
    """One object the walk visits: its id, what kind it is, whether it has children, and its own text."""

    object_id: str
    block_type: str
    has_children: bool = True
    text: str = ""

    @classmethod
    def of(cls, block: RawAPIDict) -> "_Node":
        return cls(
            str(block.get("id", "")), str(block.get("type", "")), bool(block.get("has_children")), _block_text(block)
        )


def _anchored_comment(raw: RawAPIDict, node: _Node) -> AnchoredComment:
    spans = raw.get("rich_text")
    return AnchoredComment(
        comment_id=str(raw.get("id", "")),
        discussion_id=str(raw.get("discussion_id", "")),
        block_id=node.object_id,
        block_type=node.block_type,
        author=_author(raw),
        created_time=str(raw.get("created_time", "")),
        text=rich_text_plain(cast("list[RawAPIDict]", spans)) if isinstance(spans, list) else "",
        anchor_text=node.text,
    )


def _block_text(block: RawAPIDict) -> str:
    """The block's own text, truncated — what a reader needs to know what a thread is attached to."""
    payload = block.get(str(block.get("type", "")))
    typed = cast("RawAPIDict", payload) if isinstance(payload, dict) else {}
    spans = typed.get("rich_text")
    cells = typed.get("cells")
    if isinstance(spans, list):
        text = rich_text_plain(cast("list[RawAPIDict]", spans))
    elif isinstance(cells, list):
        text = " | ".join(rich_text_plain(cast("list[RawAPIDict]", cell)) for cell in cells if isinstance(cell, list))
    else:
        text = str(typed.get("title", ""))
    flat = " ".join(text.split())
    return flat if len(flat) <= _ANCHOR_TEXT_LIMIT else f"{flat[: _ANCHOR_TEXT_LIMIT - 1]}…"


def _author(raw: RawAPIDict) -> str:
    """The comment's display name, falling back to the raw user id Notion always supplies."""
    display = raw.get("display_name")
    if isinstance(display, dict):
        resolved = cast("RawAPIDict", display).get("resolved_name")
        if isinstance(resolved, str) and resolved:
            return resolved
    created_by = raw.get("created_by")
    return str(cast("RawAPIDict", created_by).get("id", "?")) if isinstance(created_by, dict) else "?"


__all__ = [
    "DEFAULT_MAX_DEPTH",
    "UNPROVABLE_BY_THIS_API",
    "AnchoredComment",
    "CoverageGap",
    "DiscussionEnumerator",
    "NotionIncompleteEnumerationError",
    "PageDiscussions",
    "render_discussions",
]
