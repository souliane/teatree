"""Slack table rendering — native Block Kit ``table`` block + monospace fence.

Pure presentation, no Slack client or I/O: headers + rows + per-column
alignment in, a Block Kit ``table`` block and a triple-backtick monospace
fence out. The ``table`` block is the rich native rendering; the fence is the
degradation path a caller passes as the message ``text`` so the data survives
on a client that cannot render the block.

Kept beside the Slack backend (rather than in ``teatree.core``) so the block
schema stays with the platform that owns it — ``teatree.core.notify`` carries
the built ``blocks`` as opaque JSON and never needs to import this module,
which would cycle the ``teatree.core`` → ``teatree.backends.slack`` edge.
"""

from dataclasses import dataclass, field
from typing import Literal

from teatree.types import RawAPIDict

Align = Literal["left", "center", "right"]

MAX_ROWS = 100
MAX_COLS = 20
DEFAULT_FENCE_WIDTH = 72
_ELLIPSIS = "…"
_MIN_COL_WIDTH = 1
_EMPTY = "(no rows)"
_CELL_SEP = " | "
_RULE_SEP = "-+-"


def _cells(headers: list[str], rows: list[list[str]]) -> tuple[list[str], list[list[str]]]:
    """Coerce every value to ``str`` and cap columns at :data:`MAX_COLS`.

    Each row is padded/truncated to the header's column count so a ragged
    input never raises on an out-of-range index downstream.
    """
    capped_headers = [str(h) for h in headers[:MAX_COLS]]
    ncols = len(capped_headers)
    capped_rows: list[list[str]] = []
    for row in rows:
        values = [str(v) for v in row[:ncols]]
        values.extend("" for _ in range(ncols - len(values)))
        capped_rows.append(values)
    return capped_headers, capped_rows


def _aligns(alignment: list[Align] | None, ncols: int) -> list[Align]:
    """Normalize *alignment* to exactly *ncols* entries, defaulting to left."""
    resolved: list[Align] = list(alignment[:ncols]) if alignment else []
    resolved.extend("left" for _ in range(ncols - len(resolved)))
    return resolved


def _rich_text_cell(text: str, *, bold: bool) -> RawAPIDict:
    """A single Block Kit ``rich_text`` table cell."""
    style: RawAPIDict = {"bold": True} if bold else {}
    text_element: RawAPIDict = {"type": "text", "text": text}
    if style:
        text_element["style"] = style
    return {
        "type": "rich_text",
        "elements": [{"type": "rich_text_section", "elements": [text_element]}],
    }


def slack_table_block(
    headers: list[str],
    rows: list[list[str]],
    *,
    alignment: list[Align] | None = None,
) -> RawAPIDict:
    """Build a Block Kit ``table`` block from *headers* and *rows*.

    Header cells are bold ``rich_text``; data cells are plain ``rich_text``.
    Per-column alignment rides ``column_settings`` (``left`` when unspecified).
    Capped at :data:`MAX_COLS` columns and :data:`MAX_ROWS` data rows so a
    runaway list can never exceed Slack's table limits.
    """
    capped_headers, capped_rows = _cells(headers, rows)
    ncols = len(capped_headers)
    aligns = _aligns(alignment, ncols)
    block_rows: list[list[RawAPIDict]] = [[_rich_text_cell(h, bold=True) for h in capped_headers]]
    block_rows.extend([_rich_text_cell(cell, bold=False) for cell in row] for row in capped_rows[:MAX_ROWS])
    return {
        "type": "table",
        "column_settings": [{"align": align} for align in aligns],
        "rows": block_rows,
    }


def _pad(value: str, width: int, align: Align) -> str:
    if align == "right":
        return value.rjust(width)
    if align == "center":
        return value.center(width)
    return value.ljust(width)


def _truncate(value: str, width: int) -> str:
    """Ellipsis-truncate *value* to *width* without ever wrapping."""
    if len(value) <= width:
        return value
    if width <= 1:
        return _ELLIPSIS[:width]
    return value[: width - 1] + _ELLIPSIS


def _fit_widths(headers: list[str], rows: list[list[str]], max_width: int) -> list[int]:
    """Column widths whose separator-joined total fits *max_width*.

    Starts from each column's natural content width, then repeatedly shrinks
    the current widest column by one until the row fits — the widest-column-
    first budget the fence spec calls for. A column never shrinks below
    :data:`_MIN_COL_WIDTH`, so an unfittable row degrades gracefully rather
    than looping forever.
    """
    widths = [max(len(header), max((len(row[i]) for row in rows), default=0)) for i, header in enumerate(headers)]
    sep_total = len(_CELL_SEP) * (len(widths) - 1) if widths else 0

    def total() -> int:
        return sum(widths) + sep_total

    while total() > max_width and any(w > _MIN_COL_WIDTH for w in widths):
        widest = max(range(len(widths)), key=lambda i: widths[i])
        widths[widest] -= 1
    return widths


def slack_table_fence(
    headers: list[str],
    rows: list[list[str]],
    *,
    alignment: list[Align] | None = None,
    max_width: int = DEFAULT_FENCE_WIDTH,
) -> str:
    """Render a space-aligned monospace table wrapped in a ``` fence.

    Columns are padded to a common width (``max_width`` budget, over-wide
    columns ellipsis-truncated widest-first, never wrapped). Empty *rows*
    renders ``(no rows)`` inside the fence.
    """
    capped_headers, capped_rows = _cells(headers, rows)
    if not capped_rows:
        return f"```\n{_EMPTY}\n```"
    aligns = _aligns(alignment, len(capped_headers))
    widths = _fit_widths(capped_headers, capped_rows, max_width)

    def render_row(values: list[str]) -> str:
        return _CELL_SEP.join(_pad(_truncate(value, widths[i]), widths[i], aligns[i]) for i, value in enumerate(values))

    rule = _RULE_SEP.join("-" * width for width in widths)
    lines = [render_row(capped_headers), rule, *(render_row(row) for row in capped_rows)]
    body = "\n".join(lines)
    return f"```\n{body}\n```"


@dataclass(frozen=True, slots=True)
class TableMessage:
    """A table ready to post: native ``blocks`` + the fence ``text`` fallback."""

    blocks: list[RawAPIDict] = field(default_factory=list)
    fence: str = ""


def render_table_message(
    headers: list[str],
    rows: list[list[str]],
    *,
    alignment: list[Align] | None = None,
    title: str = "",
    max_width: int = DEFAULT_FENCE_WIDTH,
) -> TableMessage:
    """Build both renderings for one outbound post.

    ``blocks`` carries an optional leading ``section`` title then the
    ``table`` block; ``fence`` is the monospace degradation, title-prefixed to
    match, for the message ``text``.
    """
    blocks: list[RawAPIDict] = []
    if title:
        blocks.append({"type": "section", "text": {"type": "mrkdwn", "text": f"*{title}*"}})
    blocks.append(slack_table_block(headers, rows, alignment=alignment))
    fence = slack_table_fence(headers, rows, alignment=alignment, max_width=max_width)
    text = f"*{title}*\n{fence}" if title else fence
    return TableMessage(blocks=blocks, fence=text)


__all__ = [
    "DEFAULT_FENCE_WIDTH",
    "MAX_COLS",
    "MAX_ROWS",
    "Align",
    "TableMessage",
    "render_table_message",
    "slack_table_block",
    "slack_table_fence",
]
