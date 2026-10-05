"""Replace one exactly-located text span on a page, inside one block, keeping its formatting.

The API has no server-side text replace, so the page is walked block by block. A "slot" is one
block's ``rich_text`` (or one table cell) and its text is the exact concatenation of its runs'
``plain_text``; the old text must occur exactly once across every slot, and the edit must stay
inside one run or inside adjacent runs sharing one formatting. Only that rich text is PATCHed,
then the page is re-walked to prove the new state.
"""

import dataclasses
import difflib
import json
from typing import cast

import httpx

from teatree.backends.http_retry import CONNECT_ERRORS
from teatree.backends.notion.blocks import RICH_TEXT_LIMIT
from teatree.backends.notion.client import NotionClient
from teatree.backends.notion.errors import (
    NotionAnchorError,
    NotionBlockChangedError,
    NotionError,
    NotionWriteNotLandedError,
    NotionWriteUnverifiedError,
    describe_failure,
)
from teatree.types import RawAPIDict

#: Content that belongs to another object: another page, or a block synced from elsewhere.
_OPAQUE = frozenset({"child_page", "child_database", "synced_block"})
_SNIPPET = 40

type SlotKey = tuple[str, int | None]


@dataclasses.dataclass(frozen=True, slots=True)
class Slot:
    block: RawAPIDict
    cell: int | None = None

    @property
    def block_id(self) -> str:
        return str(self.block.get("id", ""))

    @property
    def kind(self) -> str:
        return str(self.block.get("type", ""))

    @property
    def key(self) -> SlotKey:
        return (self.block_id, self.cell)

    @property
    def runs(self) -> list[RawAPIDict]:
        if self.cell is None:
            return cast("list[RawAPIDict]", self._payload["rich_text"])
        return self._cells[self.cell]

    @property
    def text(self) -> str:
        return "".join(str(run.get("plain_text", "")) for run in self.runs)

    @property
    def label(self) -> str:
        return f"{self.block_id} ({self.kind}{'' if self.cell is None else f', cell {self.cell}'})"

    def payload(self, runs: list[RawAPIDict]) -> RawAPIDict:
        if self.cell is None:
            return {self.kind: {"rich_text": runs}}
        cells = [list(cell) for cell in self._cells]
        cells[self.cell] = runs
        return {self.kind: {"cells": cells}}

    @property
    def _payload(self) -> RawAPIDict:
        return cast("RawAPIDict", self.block[self.kind])

    @property
    def _cells(self) -> list[list[RawAPIDict]]:
        return cast("list[list[RawAPIDict]]", self._payload["cells"])


@dataclasses.dataclass(frozen=True, slots=True)
class ReplacePlan:
    page_id: str
    slot: Slot
    old: str
    new: str
    after: str
    runs: list[RawAPIDict]
    expected_counts: tuple[int, int]
    already_applied: bool = False

    @property
    def before(self) -> str:
        return self.slot.text

    def diff(self) -> str:
        lines = difflib.unified_diff(
            self.before.splitlines(), self.after.splitlines(), f"{self.slot.label} before", "after", lineterm=""
        )
        return "\n".join(lines)


def occurrences(text: str, needle: str) -> list[int]:
    """Every start index of *needle* in *text*, overlapping ones included."""
    found: list[int] = []
    start = text.find(needle) if needle else -1
    while start != -1:
        found.append(start)
        start = text.find(needle, start + 1)
    return found


class PageText:
    def __init__(self, client: NotionClient) -> None:
        self.client = client
        self.unsearched: list[str] = []

    def locate(self, page_id: str, text: str) -> tuple[Slot, int, list[Slot]]:
        """The one slot holding *text* and where in it, plus every slot walked — or a refusal naming the count."""
        slots = self.slots(page_id)
        matches = [(slot, start) for slot in slots for start in occurrences(slot.text, text)]
        if len(matches) != 1:
            raise NotionAnchorError(_count_refusal(page_id, text, slots, matches, self.unsearched))
        slot, start = matches[0]
        return slot, start, slots

    def plan(self, page_id: str, *, old: str, new: str) -> ReplacePlan:
        slot, start, slots = self.locate(page_id, old)
        if _inside_new_text(slot.text, start, old=old, new=new):
            counts = _counts([candidate.text for candidate in slots], old, new)
            return ReplacePlan(page_id, slot, old, new, slot.text, slot.runs, counts, already_applied=True)
        runs = _replaced_runs(slot, start, start + len(old), new)
        after = "".join(str(run.get("plain_text", "")) for run in runs)
        expected = [after if candidate.key == slot.key else candidate.text for candidate in slots]
        return ReplacePlan(page_id, slot, old, new, after, runs, _counts(expected, old, new))

    def ensure_unchanged(self, plan: ReplacePlan) -> RawAPIDict:
        """The block as it reads now, or a refusal when it was edited since *plan* read it."""
        current = self.client.get_block(plan.slot.block_id)
        if _block_segments(current) != _block_segments(plan.slot.block):
            msg = (
                f"block changed: {plan.slot.label} was edited since it was read, so this replace would overwrite "
                "that edit. Nothing was written — re-run it, with --dry-run first, against the current text."
            )
            raise NotionBlockChangedError(msg)
        return current

    def apply(self, plan: ReplacePlan) -> tuple[int, int]:
        current = self.ensure_unchanged(plan)
        try:
            self.client.update_block(plan.slot.block_id, Slot(current, plan.slot.cell).payload(plan.runs))
        except (NotionError, httpx.HTTPError) as exc:
            if not _may_have_landed(exc):
                raise
            raise _unverified(plan, exc) from exc
        try:
            slots = self.slots(plan.page_id)
        except (NotionError, httpx.HTTPError) as exc:
            raise _unverified(plan, exc) from exc
        landed = next((slot for slot in slots if slot.key == plan.slot.key), None)
        counts = _counts([slot.text for slot in slots], plan.old, plan.new)
        if landed is None or _segments(landed.runs) != _segments(plan.runs) or counts != plan.expected_counts:
            reads = landed.text if landed is not None else None
            msg = (
                f"the replace reported success but {plan.slot.label} reads {reads!r} on re-fetch, expected "
                f"{plan.after!r} with its formatting; old/new text now occur {counts} time(s), expected "
                f"{plan.expected_counts} — treat the write as failed."
            )
            raise NotionWriteNotLandedError(msg)
        return counts

    def slots(self, block_id: str) -> list[Slot]:
        self.unsearched = []
        return self._walk(block_id)

    def _walk(self, block_id: str) -> list[Slot]:
        found: list[Slot] = []
        for block in self.client.list_block_children(block_id):
            found.extend(_slots_of(block))
            if block.get("type") in _OPAQUE:
                self.unsearched.append(f"{block.get('type')} {block.get('id')}")
            elif block.get("has_children"):
                found.extend(self._walk(str(block.get("id", ""))))
        return found


def _slots_of(block: RawAPIDict) -> list[Slot]:
    payload = block.get(str(block.get("type", "")))
    if not isinstance(payload, dict):
        return []
    cells = cast("RawAPIDict", payload).get("cells")
    if isinstance(cells, list):
        return [Slot(block, index) for index in range(len(cells))]
    return [Slot(block)] if isinstance(cast("RawAPIDict", payload).get("rich_text"), list) else []


def _may_have_landed(exc: Exception) -> bool:
    """A 5xx, or a transport failure once the connection opened; a 4xx verdict or a guard refusal wrote nothing."""
    if isinstance(exc, httpx.HTTPStatusError):
        return exc.response.status_code >= httpx.codes.INTERNAL_SERVER_ERROR
    return isinstance(exc, httpx.TransportError) and not isinstance(exc, CONNECT_ERRORS)


def _unverified(plan: ReplacePlan, exc: Exception) -> NotionWriteUnverifiedError:
    return NotionWriteUnverifiedError(
        f"the replace of {plan.slot.label} was sent and may have landed, but {describe_failure(exc)} followed, so "
        "the page state is unknown — re-read the page before retrying. The ledger records the edit as unverified."
    )


def _inside_new_text(text: str, start: int, *, old: str, new: str) -> bool:
    """Whether the one match of *old* sits inside *new* already written there — a retry of an applied replace."""
    return any(text.startswith(new, start - offset) for offset in occurrences(new, old) if start >= offset)


def _block_segments(block: RawAPIDict) -> list[list[tuple[str, str]]]:
    return [_segments(slot.runs) for slot in _slots_of(block)]


def _counts(texts: list[str], old: str, new: str) -> tuple[int, int]:
    return (
        sum(len(occurrences(text, old)) for text in texts),
        sum(len(occurrences(text, new)) for text in texts),
    )


def _count_refusal(
    page_id: str, old: str, slots: list[Slot], matches: list[tuple[Slot, int]], unsearched: list[str]
) -> str:
    if matches:
        where = "; ".join(f"{slot.label}: …{_snippet(slot.text, start, len(old))}…" for slot, start in matches)
        return (
            f"the text occurs {len(matches)} times on page {page_id}, not exactly once — widen it until it is "
            f"unique. Found at {where}. Nothing was written."
        )
    spans_blocks = old in "\n".join(slot.text for slot in slots)
    if spans_blocks:
        reason = "it spans several blocks, and a replace edits one block — replace each block's part separately"
    elif unsearched:
        reason = (
            f"this page also holds content that is not searched — {', '.join(unsearched)} — whose text is owned "
            "elsewhere; if the text is there, edit it at its source"
        )
    else:
        reason = "check the exact characters, including spaces and punctuation"
    return f"the text occurs 0 times inside any one block of page {page_id}: {reason}. Nothing was written."


def _snippet(text: str, start: int, length: int) -> str:
    return text[max(0, start - _SNIPPET) : start + length + _SNIPPET].replace("\n", " ")


def _replaced_runs(slot: Slot, start: int, end: int, new: str) -> list[RawAPIDict]:
    runs = slot.runs
    offsets: list[int] = []
    position = 0
    for run in runs:
        offsets.append(position)
        position += len(str(run.get("plain_text", "")))
    first = _run_at(runs, offsets, start)
    last = _run_at(runs, offsets, end - 1)
    span = runs[first : last + 1]
    formats = {_format_of(run) for run in span}
    if any(run.get("type") != "text" for run in span) or len(formats) > 1:
        msg = (
            f"the old text in {slot.label} crosses runs with different formatting, or a mention/equation "
            f"({sorted(formats)}); replacing it would flatten that formatting. Choose old text inside one "
            "formatted run, or edit this one by hand. Nothing was written."
        )
        raise NotionAnchorError(msg)
    merged = "".join(str(run.get("plain_text", "")) for run in span)
    content = merged[: start - offsets[first]] + new + merged[end - offsets[first] :]
    if len(content) > RICH_TEXT_LIMIT:
        msg = (
            f"the edited run in {slot.label} would hold {len(content)} characters, over Notion's "
            f"{RICH_TEXT_LIMIT}-character run limit. Nothing was written."
        )
        raise NotionAnchorError(msg)
    text = cast("RawAPIDict", span[0].get("text") or {})
    edited = {**span[0], "plain_text": content, "text": {**text, "content": content}}
    return [*runs[:first], *([edited] if content else []), *runs[last + 1 :]]


def _run_at(runs: list[RawAPIDict], offsets: list[int], position: int) -> int:
    return next(
        index
        for index, run in enumerate(runs)
        if offsets[index] <= position < offsets[index] + len(str(run.get("plain_text", "")))
    )


def _format_of(run: RawAPIDict) -> str:
    text = run.get("text")
    link = cast("RawAPIDict", text).get("link") if isinstance(text, dict) else None
    annotations = run.get("annotations")
    set_marks = (
        {key: value for key, value in cast("RawAPIDict", annotations).items() if value not in {False, None, "default"}}
        if isinstance(annotations, dict)
        else {}
    )
    return json.dumps([run.get("type"), set_marks, link], sort_keys=True)


def _segments(runs: list[RawAPIDict]) -> list[tuple[str, str]]:
    """Text grouped by formatting, so Notion coalescing or splitting equal runs is not a change."""
    merged: list[tuple[str, str]] = []
    for run in runs:
        style, text = _format_of(run), str(run.get("plain_text", ""))
        if merged and merged[-1][0] == style:
            merged[-1] = (style, merged[-1][1] + text)
        elif text:
            merged.append((style, text))
    return merged
