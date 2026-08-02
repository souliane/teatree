"""The Markdown subset the write path accepts, and what it refuses."""

from typing import Any, cast

import pytest

from teatree.backends.notion.blocks import RICH_TEXT_LIMIT, build_blocks, rich_text
from teatree.backends.notion.errors import NotionUnsupportedMarkdownError


def _types(markdown: str) -> list[str]:
    return [str(block["type"]) for block in build_blocks(markdown)]


def _payload(markdown: str, key: str) -> dict[str, Any]:
    """The type payload of the first block the builder produced."""
    return cast("dict[str, Any]", build_blocks(markdown)[0][key])


class TestBlockShapes:
    @pytest.mark.parametrize(
        ("markdown", "expected"),
        [
            ("# Title", "heading_1"),
            ("## Section", "heading_2"),
            ("### Sub", "heading_3"),
            ("plain prose", "paragraph"),
            ("- a bullet", "bulleted_list_item"),
            ("1. an item", "numbered_list_item"),
            ("- [x] done", "to_do"),
            ("> quoted", "quote"),
            ("---", "divider"),
        ],
    )
    def test_each_line_shape_maps_to_its_block(self, markdown: str, expected: str) -> None:
        assert _types(markdown) == [expected]

    def test_a_toggle_attribute_on_a_heading_makes_it_collapsible(self) -> None:
        payload = _payload('## 🔧 notes {toggle="true"}', "heading_2")

        assert payload["is_toggleable"] is True
        assert payload["rich_text"][0]["text"]["content"] == "🔧 notes"

    def test_a_fenced_block_keeps_its_body_and_maps_the_language(self) -> None:
        payload = _payload("```gherkin\nGiven a loan\nWhen priced\n```", "code")

        assert payload["language"] == "gherkin"
        assert payload["rich_text"][0]["text"]["content"] == "Given a loan\nWhen priced"

    def test_an_unknown_fence_language_degrades_rather_than_failing_the_write(self) -> None:
        assert _payload("```wat\nx\n```", "code")["language"] == "plain text"

    def test_a_pipe_table_becomes_a_table_block_with_its_rows(self) -> None:
        table = _payload("| # | Item | Why |\n| --- | --- | --- |\n| 1 | TICKET-1 | drift |", "table")
        assert table["table_width"] == 3
        assert len(table["children"]) == 2, "the --- separator row is not a data row"
        assert table["children"][1]["table_row"]["cells"][1][0]["text"]["content"] == "TICKET-1"

    def test_a_details_block_becomes_a_toggle_carrying_its_body(self) -> None:
        toggle = _payload("<details>\n<summary>Full Gherkin</summary>\n\n- one\n\n</details>", "toggle")
        assert toggle["rich_text"][0]["text"]["content"] == "Full Gherkin"
        assert [child["type"] for child in toggle["children"]] == ["bulleted_list_item"]


class TestInlineFormatting:
    @pytest.mark.parametrize(
        ("markdown", "annotation"),
        [("**bold**", "bold"), ("*slanted*", "italic"), ("`code`", "code"), ("~~gone~~", "strikethrough")],
    )
    def test_inline_markers_become_annotations(self, markdown: str, annotation: str) -> None:
        spans = rich_text(markdown)

        assert cast("dict[str, Any]", spans[0]["annotations"])[annotation] is True

    def test_a_link_keeps_its_label_and_href(self) -> None:
        spans = rich_text("see [MR !1](https://example.test/mr/1) for detail")

        texts = [cast("dict[str, Any]", span["text"]) for span in spans]
        assert [text["content"] for text in texts] == ["see ", "MR !1", " for detail"]
        assert texts[1]["link"] == {"url": "https://example.test/mr/1"}

    def test_a_run_longer_than_notions_limit_is_split_not_rejected(self) -> None:
        spans = rich_text("x" * (RICH_TEXT_LIMIT + 10))

        assert len(spans) == 2
        assert len(cast("dict[str, Any]", spans[0]["text"])["content"]) == RICH_TEXT_LIMIT


class TestRefusals:
    def test_unmapped_html_is_refused_naming_the_line(self) -> None:
        markdown = '### Notes\n\n<callout icon="warning">watch out</callout>\n'

        with pytest.raises(NotionUnsupportedMarkdownError, match="line 3: <callout>"):
            build_blocks(markdown)

    def test_an_empty_body_is_an_empty_block_list_not_an_error(self) -> None:
        assert build_blocks("\n\n  \n") == []
