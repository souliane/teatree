"""Build Notion blocks from a Markdown body — the write path's input stage.

Deliberately a **defined subset**, not a best-effort parser. A construct this
builder has no faithful Notion mapping for raises
:class:`~teatree.backends.notion.errors.NotionUnsupportedMarkdownError` naming the
line, because a section that silently lost its table is worse than a run that
refused and said which line it could not represent. A caller that genuinely
needs an exotic block hands raw block JSON instead (``--blocks-file``), so the
subset is never a dead end.

Supported: ATX headings (``#``…``###``, optionally toggle via a trailing
``{toggle="true"}`` attribute), paragraphs, bulleted and numbered list items,
task list items, block quotes, fenced code, ``---`` dividers, pipe tables, and
``<details><summary>…`` toggles. Inline: ``**bold**``, ``*italic*``,
``` `code` ```, ``~~strike~~``, and ``[text](url)``.
"""

import re
from typing import ClassVar

from teatree.backends.notion.errors import NotionUnsupportedMarkdownError
from teatree.types import RawAPIDict

#: Notion refuses a single rich-text run longer than this.
RICH_TEXT_LIMIT = 2000

_HEADING = re.compile(r"^(#{1,3})\s+(.*)$")
_BULLET = re.compile(r"^[-*]\s+(?!\[[ xX]\]\s)(.*)$")
_TASK = re.compile(r"^[-*]\s+\[([ xX])\]\s+(.*)$")
_NUMBERED = re.compile(r"^\d+[.)]\s+(.*)$")
_QUOTE = re.compile(r"^>\s?(.*)$")
_FENCE = re.compile(r"^```(\S*)\s*$")
_DIVIDER = re.compile(r"^(-{3,}|\*{3,}|_{3,})$")
_TABLE_DIVIDER = re.compile(r"^\|[\s:|-]+\|$")
_SUMMARY = re.compile(r"^<summary>(.*)</summary>$")
_TOGGLE_ATTR = re.compile(r"\s*\{toggle\s*=\s*\"?true\"?\}\s*$", re.IGNORECASE)
# A line OPENING with a block tag, whether or not it also closes on that line —
# `<callout icon="x">text</callout>` is one line and must still be refused.
_HTML_TAG = re.compile(r"^</?([a-zA-Z][\w-]*)[\s>/]")
_HANDLED_HTML_TAGS = frozenset({"details", "summary"})

_INLINE = re.compile(
    r"(?P<link>\[(?P<label>[^\]]*)\]\((?P<href>[^)]*)\))"
    r"|(?P<code>`[^`]+`)"
    r"|(?P<bold>\*\*[^*]+\*\*)"
    r"|(?P<strike>~~[^~]+~~)"
    r"|(?P<italic>\*[^*]+\*)"
)

_ANNOTATION_KEYS = ("bold", "italic", "strikethrough", "code")


def rich_text(markdown: str) -> list[RawAPIDict]:
    """Parse inline Markdown into a Notion ``rich_text`` array.

    Runs longer than :data:`RICH_TEXT_LIMIT` are split across successive spans
    with the same annotations, so a long paragraph is never rejected by Notion
    for a limit the caller cannot see.
    """
    spans: list[RawAPIDict] = []
    cursor = 0
    for match in _INLINE.finditer(markdown):
        spans.extend(_plain_spans(markdown[cursor : match.start()]))
        spans.extend(_styled_spans(match))
        cursor = match.end()
    spans.extend(_plain_spans(markdown[cursor:]))
    return spans


def _styled_spans(match: re.Match[str]) -> list[RawAPIDict]:
    if match.group("link") is not None:
        return _spans(match.group("label"), href=match.group("href"))
    for key, marker in (("code", "`"), ("bold", "**"), ("strike", "~~"), ("italic", "*")):
        raw = match.group(key)
        if raw is not None:
            annotation = "strikethrough" if key == "strike" else key
            return _spans(raw.strip(marker), annotation=annotation)
    return []


def literal_rich_text(text: str) -> list[RawAPIDict]:
    """Wrap *text* verbatim — no inline Markdown, chunked to Notion's per-run cap.

    The counterpart to :func:`rich_text` for the surfaces where the value IS the
    value: a page property and a comment marker are matched character for
    character by their callers, so parsing ``**`` or ``[a](b)`` out of them would
    make the round trip lossy exactly where an idempotency key is compared.
    """
    return [
        {"type": "text", "text": {"content": text[start : start + RICH_TEXT_LIMIT]}}
        for start in range(0, len(text), RICH_TEXT_LIMIT)
    ]


def _plain_spans(text: str) -> list[RawAPIDict]:
    return _spans(text) if text else []


def _spans(text: str, *, annotation: str = "", href: str = "") -> list[RawAPIDict]:
    annotations = {key: key == annotation for key in _ANNOTATION_KEYS}
    chunks = [text[i : i + RICH_TEXT_LIMIT] for i in range(0, len(text), RICH_TEXT_LIMIT)] or [text]
    return [
        {
            "type": "text",
            "text": {"content": chunk, "link": {"url": href} if href else None},
            "annotations": annotations,
        }
        for chunk in chunks
    ]


def heading_block(text: str, *, level: int = 2, toggle: bool = False) -> RawAPIDict:
    """Build one heading block; *toggle* makes it a collapsible toggle heading."""
    return {
        "object": "block",
        "type": f"heading_{level}",
        f"heading_{level}": {"rich_text": rich_text(text), "is_toggleable": toggle},
    }


class MarkdownBlockBuilder:
    """Convert a Markdown body into the Notion block list an append call takes."""

    _SIMPLE: ClassVar[dict[str, str]] = {
        "bulleted": "bulleted_list_item",
        "numbered": "numbered_list_item",
        "quote": "quote",
    }

    def __init__(self) -> None:
        self._lines: list[str] = []
        self._index = 0

    def build(self, markdown: str) -> list[RawAPIDict]:
        self._lines = markdown.replace("\r\n", "\n").split("\n")
        self._index = 0
        blocks: list[RawAPIDict] = []
        while self._index < len(self._lines):
            block = self._next_block()
            if block is not None:
                blocks.append(block)
        return blocks

    def _next_block(self) -> RawAPIDict | None:
        stripped = self._lines[self._index].strip()
        line_number = self._index + 1
        self._index += 1
        if not stripped:
            return None
        for handler in (self._fence, self._details, self._table, self._heading, self._list_item, self._divider):
            block = handler(stripped)
            if block is not None:
                return block
        self._reject_unmapped_html(stripped, line_number)
        return {"object": "block", "type": "paragraph", "paragraph": {"rich_text": rich_text(stripped)}}

    @staticmethod
    def _reject_unmapped_html(stripped: str, line_number: int) -> None:
        """Refuse a raw HTML block tag with no Notion mapping, naming the line.

        Falling through to a paragraph would embed the literal markup in the page
        and quietly lose whatever the tag expressed (a callout, a table). The
        caller's escape hatch is raw block JSON, not a guessed mapping.
        """
        match = _HTML_TAG.match(stripped)
        if match is None or match.group(1).lower() in _HANDLED_HTML_TAGS:
            return
        msg = (
            f"line {line_number}: <{match.group(1)}> has no Notion block mapping — "
            f"{stripped[:120]!r}. Rewrite it in the supported Markdown subset, or pass "
            "raw Notion block JSON instead of Markdown."
        )
        raise NotionUnsupportedMarkdownError(msg)

    @staticmethod
    def _heading(stripped: str) -> RawAPIDict | None:
        match = _HEADING.match(stripped)
        if match is None:
            return None
        text = match.group(2).strip()
        toggle = bool(_TOGGLE_ATTR.search(text))
        return heading_block(_TOGGLE_ATTR.sub("", text), level=len(match.group(1)), toggle=toggle)

    def _list_item(self, stripped: str) -> RawAPIDict | None:
        task = _TASK.match(stripped)
        if task is not None:
            return {
                "object": "block",
                "type": "to_do",
                "to_do": {"rich_text": rich_text(task.group(2)), "checked": task.group(1).lower() == "x"},
            }
        for name, pattern in (("bulleted", _BULLET), ("numbered", _NUMBERED), ("quote", _QUOTE)):
            match = pattern.match(stripped)
            if match is not None:
                kind = self._SIMPLE[name]
                return {"object": "block", "type": kind, kind: {"rich_text": rich_text(match.group(1))}}
        return None

    @staticmethod
    def _divider(stripped: str) -> RawAPIDict | None:
        return {"object": "block", "type": "divider", "divider": {}} if _DIVIDER.match(stripped) else None

    def _fence(self, stripped: str) -> RawAPIDict | None:
        match = _FENCE.match(stripped)
        if match is None:
            return None
        body: list[str] = []
        while self._index < len(self._lines) and not _FENCE.match(self._lines[self._index].strip()):
            body.append(self._lines[self._index])
            self._index += 1
        self._index += 1
        return {
            "object": "block",
            "type": "code",
            "code": {
                "rich_text": _spans("\n".join(body)),
                "language": _notion_language(match.group(1)),
            },
        }

    def _details(self, stripped: str) -> RawAPIDict | None:
        if stripped != "<details>":
            return None
        summary = ""
        body: list[str] = []
        while self._index < len(self._lines) and self._lines[self._index].strip() != "</details>":
            current = self._lines[self._index].strip()
            self._index += 1
            match = _SUMMARY.match(current)
            if match is not None:
                summary = match.group(1)
                continue
            body.append(current)
        self._index += 1
        return {
            "object": "block",
            "type": "toggle",
            "toggle": {"rich_text": rich_text(summary), "children": MarkdownBlockBuilder().build("\n".join(body))},
        }

    def _table(self, stripped: str) -> RawAPIDict | None:
        if not stripped.startswith("|") or not stripped.endswith("|"):
            return None
        rows = [_table_cells(stripped)]
        while self._index < len(self._lines):
            candidate = self._lines[self._index].strip()
            if not candidate.startswith("|"):
                break
            self._index += 1
            if _TABLE_DIVIDER.match(candidate):
                continue
            rows.append(_table_cells(candidate))
        width = max(len(row) for row in rows)
        return {
            "object": "block",
            "type": "table",
            "table": {
                "table_width": width,
                "has_column_header": True,
                "has_row_header": False,
                "children": [_table_row(row, width) for row in rows],
            },
        }


def _table_cells(line: str) -> list[str]:
    return [cell.strip() for cell in line.strip().strip("|").split("|")]


def _table_row(cells: list[str], width: int) -> RawAPIDict:
    padded = [*cells, *([""] * (width - len(cells)))]
    return {
        "object": "block",
        "type": "table_row",
        "table_row": {"cells": [rich_text(cell) for cell in padded]},
    }


#: Notion validates ``code.language`` against a closed list; anything outside it
#: is rejected wholesale, so an unknown fence tag degrades to plain text rather
#: than failing a write over a label.
_NOTION_LANGUAGES = frozenset(
    {
        "bash",
        "css",
        "diff",
        "docker",
        "gherkin",
        "go",
        "graphql",
        "html",
        "java",
        "javascript",
        "json",
        "kotlin",
        "markdown",
        "mermaid",
        "plain text",
        "python",
        "ruby",
        "rust",
        "shell",
        "sql",
        "typescript",
        "xml",
        "yaml",
    }
)

_LANGUAGE_ALIASES = {
    "js": "javascript",
    "py": "python",
    "sh": "shell",
    "ts": "typescript",
    "yml": "yaml",
    "zsh": "shell",
}


def _notion_language(tag: str) -> str:
    normalized = _LANGUAGE_ALIASES.get(tag.lower(), tag.lower())
    return normalized if normalized in _NOTION_LANGUAGES else "plain text"


def build_blocks(markdown: str) -> list[RawAPIDict]:
    """Convert a Markdown body to Notion blocks, refusing what it cannot represent."""
    blocks = MarkdownBlockBuilder().build(markdown)
    if not blocks and markdown.strip():
        msg = f"the body produced no Notion blocks: {markdown.strip()[:120]!r}"
        raise NotionUnsupportedMarkdownError(msg)
    return blocks
