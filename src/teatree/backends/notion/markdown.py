"""Render Notion blocks as Markdown so an agent can read a page as text.

The read side of the round trip. Notion's API returns a block tree, not the
Markdown the interactive connector hands an agent, so a headless reader needs a
renderer of its own. This one is deliberately lossy-but-honest: a block type it
has no Markdown for renders as its plain text prefixed with the type name, so
content is never silently dropped and a reader can see what it is looking at.

The **write** path does not go through here. Section replacement matches on
block ids and heading blocks (:mod:`teatree.backends.notion.sections`), so a
gap in this renderer can never mis-target a write.
"""

from collections.abc import Callable
from typing import cast

from teatree.types import RawAPIDict

type ChildrenLookup = Callable[[str], list[RawAPIDict]]

_HEADING_PREFIX = {"heading_1": "#", "heading_2": "##", "heading_3": "###"}

_ANNOTATION_WRAPPERS = (("code", "`"), ("bold", "**"), ("italic", "*"), ("strikethrough", "~~"))

# Block types whose Markdown depends only on their own text. A trailing ``""``
# is the blank line that separates a block-level construct from the next one.
_TEMPLATES: dict[str, list[str]] = {
    "paragraph": ["{text}", ""],
    "bulleted_list_item": ["- {text}"],
    "toggle": ["- {text}"],
    "quote": ["> {text}", ""],
    "callout": ["> {text}", ""],
    "divider": ["---", ""],
    # A ``table`` renders as nothing itself — its ``table_row`` children carry
    # every cell, and they are reached through the has-children walk.
    "table": [],
}

_CONTEXTUAL_RENDERERS: dict[str, Callable[[str, RawAPIDict, int], list[str]]] = {
    "numbered_list_item": lambda text, _payload, ordinal: [f"{ordinal}. {text}"],
    "to_do": lambda text, payload, _ordinal: [f"- [{'x' if payload.get('checked') else ' '}] {text}"],
    "code": lambda text, payload, _ordinal: [f"```{payload.get('language', '')}", text, "```", ""],
    "table_row": lambda _text, payload, _ordinal: [_table_row(payload)],
}


def _table_row(payload: RawAPIDict) -> str:
    cells = payload.get("cells")
    rendered = (
        [rich_text_to_markdown(cast("list[RawAPIDict]", cell)) for cell in cells] if isinstance(cells, list) else []
    )
    return "| " + " | ".join(rendered) + " |"


def rich_text_to_markdown(rich_text: list[RawAPIDict]) -> str:
    """Flatten one Notion ``rich_text`` array into inline Markdown."""
    return "".join(_span_to_markdown(span) for span in rich_text)


def rich_text_plain(rich_text: list[RawAPIDict]) -> str:
    """Flatten a ``rich_text`` array to its bare text, annotations dropped."""
    return "".join(str(span.get("plain_text", "")) for span in rich_text)


def _span_to_markdown(span: RawAPIDict) -> str:
    text = str(span.get("plain_text", ""))
    if not text:
        return ""
    annotations = span.get("annotations")
    if isinstance(annotations, dict):
        typed = cast("RawAPIDict", annotations)
        for key, wrapper in _ANNOTATION_WRAPPERS:
            if typed.get(key):
                text = f"{wrapper}{text}{wrapper}"
    link = span.get("href")
    return f"[{text}]({link})" if link else text


class BlockMarkdownRenderer:
    """Render a fetched block tree to Markdown.

    *children_of* is injected so the renderer can walk nested blocks (a toggle's
    body, a table's rows, a nested list) without owning an HTTP client — the same
    seam that lets a test render a whole tree with no network at all.
    """

    def __init__(self, children_of: ChildrenLookup) -> None:
        self._children_of = children_of

    def render(self, blocks: list[RawAPIDict], *, indent: int = 0) -> str:
        lines: list[str] = []
        numbering = 0
        for block in blocks:
            block_type = str(block.get("type", ""))
            numbering = numbering + 1 if block_type == "numbered_list_item" else 0
            lines.extend(self._render_block(block, indent=indent, ordinal=numbering))
        return "\n".join(lines)

    def _render_block(self, block: RawAPIDict, *, indent: int, ordinal: int) -> list[str]:
        block_type = str(block.get("type", ""))
        body = block.get(block_type)
        payload = cast("RawAPIDict", body) if isinstance(body, dict) else {}
        pad = "  " * indent
        rendered = [pad + line for line in self._lines_for(block_type, payload, ordinal=ordinal)]
        if block.get("has_children") and block_type != "table":
            nested = self.render(self._children_of(str(block.get("id", ""))), indent=indent + 1)
            if nested:
                rendered.append(nested)
        return rendered

    def _lines_for(self, block_type: str, payload: RawAPIDict, *, ordinal: int) -> list[str]:
        text = rich_text_to_markdown(self._rich_text(payload))
        if block_type in _HEADING_PREFIX:
            return [f"{_HEADING_PREFIX[block_type]} {text}", ""]
        if block_type in _CONTEXTUAL_RENDERERS:
            return _CONTEXTUAL_RENDERERS[block_type](text, payload, ordinal)
        template = _TEMPLATES.get(block_type)
        if template is None:
            # An unrendered type keeps its text AND announces itself, so a reader
            # sees what is there rather than a silent gap.
            return [f"<{block_type}> {text}".rstrip()]
        return [line.format(text=text) for line in template]

    @staticmethod
    def _rich_text(payload: RawAPIDict) -> list[RawAPIDict]:
        rich_text = payload.get("rich_text")
        return cast("list[RawAPIDict]", rich_text) if isinstance(rich_text, list) else []
