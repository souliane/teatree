"""The Markdown subset the write path accepts, and what it refuses."""

from typing import Any, cast

import pytest

from teatree.backends.notion.blocks import RICH_TEXT_LIMIT, build_blocks, copyable_blocks, rich_text
from teatree.backends.notion.errors import NotionUncopyableBlockError, NotionUnsupportedMarkdownError


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


SERVER_OWNED = frozenset(
    {
        "id",
        "parent",
        "created_time",
        "created_by",
        "last_edited_time",
        "last_edited_by",
        "archived",
        "in_trash",
        "has_children",
    }
)


def _fetched(block_id: str, kind: str, payload: dict[str, Any], *, has_children: bool = False) -> dict[str, Any]:
    """One block in the shape Notion reads BACK, server-owned fields and all."""
    return {
        "object": "block",
        "id": block_id,
        "parent": {"type": "page_id", "page_id": "pg-1"},
        "created_time": "2026-01-01T00:00:00.000Z",
        "created_by": {"object": "user", "id": "user-1"},
        "last_edited_time": "2026-01-02T00:00:00.000Z",
        "last_edited_by": {"object": "user", "id": "user-1"},
        "archived": False,
        "in_trash": False,
        "has_children": has_children,
        "type": kind,
        kind: payload,
    }


def _paragraph(block_id: str, text: str) -> dict[str, Any]:
    return _fetched(block_id, "paragraph", {"rich_text": [{"type": "text", "text": {"content": text}}]})


def _lookup(tree: dict[str, list[dict[str, Any]]]) -> Any:
    return lambda block_id: tree.get(block_id, [])


def _server_owned_keys(value: Any) -> set[str]:
    if isinstance(value, dict):
        nested = [_server_owned_keys(item) for item in value.values()]
        return set(SERVER_OWNED & value.keys()).union(*nested, set())
    if isinstance(value, list):
        return set().union(*(_server_owned_keys(item) for item in value), set())
    return set()


class TestCopyableBlocks:
    def test_a_nested_tree_comes_back_inline_and_carries_no_server_fields(self) -> None:
        toggle = _fetched(
            "blk-1", "toggle", {"rich_text": [{"type": "text", "text": {"content": "why"}}]}, has_children=True
        )
        tree = {
            "blk-1": [
                _fetched(
                    "blk-2",
                    "bulleted_list_item",
                    {"rich_text": [{"type": "text", "text": {"content": "because"}}]},
                    has_children=True,
                )
            ],
            "blk-2": [_paragraph("blk-3", "deepest")],
        }

        copied = copyable_blocks(_lookup(tree), [toggle])

        assert copied == [
            {
                "object": "block",
                "type": "toggle",
                "toggle": {
                    "rich_text": [{"type": "text", "text": {"content": "why"}}],
                    "children": [
                        {
                            "object": "block",
                            "type": "bulleted_list_item",
                            "bulleted_list_item": {
                                "rich_text": [{"type": "text", "text": {"content": "because"}}],
                                "children": [
                                    {
                                        "object": "block",
                                        "type": "paragraph",
                                        "paragraph": {"rich_text": [{"type": "text", "text": {"content": "deepest"}}]},
                                    }
                                ],
                            },
                        }
                    ],
                },
            }
        ]
        assert _server_owned_keys(copied) == set()

    def test_a_table_keeps_its_rows(self) -> None:
        table = _fetched(
            "tbl-1", "table", {"table_width": 2, "has_column_header": True, "has_row_header": False}, has_children=True
        )
        row = _fetched(
            "row-1",
            "table_row",
            {
                "cells": [
                    [{"type": "text", "text": {"content": "Rate"}}],
                    [{"type": "text", "text": {"content": "3.4%"}}],
                ]
            },
        )

        copied = copyable_blocks(_lookup({"tbl-1": [row]}), [table])

        payload = cast("dict[str, Any]", copied[0]["table"])
        assert payload["children"] == [
            {
                "object": "block",
                "type": "table_row",
                "table_row": {
                    "cells": [
                        [{"type": "text", "text": {"content": "Rate"}}],
                        [{"type": "text", "text": {"content": "3.4%"}}],
                    ]
                },
            }
        ]
        assert payload["table_width"] == 2

    def test_the_source_block_is_left_untouched(self) -> None:
        toggle = _fetched("blk-1", "toggle", {"rich_text": []}, has_children=True)

        copyable_blocks(_lookup({"blk-1": [_paragraph("blk-2", "child")]}), [toggle])

        assert "children" not in toggle["toggle"]

    @pytest.mark.parametrize("kind", ["child_page", "child_database", "synced_block", "unsupported"])
    def test_a_type_that_cannot_be_re_posted_is_refused_naming_it(self, kind: str) -> None:
        block = _fetched("blk-9", kind, {})

        with pytest.raises(NotionUncopyableBlockError, match=f"blk-9.*{kind}"):
            copyable_blocks(_lookup({}), [block])

    def test_a_refusal_nested_under_a_copyable_parent_still_stops_the_copy(self) -> None:
        toggle = _fetched("blk-1", "toggle", {"rich_text": []}, has_children=True)
        tree = {"blk-1": [_fetched("blk-7", "child_page", {"title": "Appendix"})]}

        with pytest.raises(NotionUncopyableBlockError, match="child_page"):
            copyable_blocks(_lookup(tree), [toggle])

    @pytest.mark.parametrize("kind", ["image", "file"])
    def test_an_external_asset_is_kept(self, kind: str) -> None:
        payload = {"type": "external", "external": {"url": "https://example.test/logo.png"}, "caption": []}

        copied = copyable_blocks(_lookup({}), [_fetched("blk-4", kind, payload)])

        assert copied == [{"object": "block", "type": kind, kind: payload}]

    @pytest.mark.parametrize("kind", ["image", "file"])
    def test_a_notion_hosted_asset_is_refused_because_its_url_expires(self, kind: str) -> None:
        payload = {
            "type": "file",
            "file": {"url": "https://s3.notion.test/signed?x=1", "expiry_time": "2026-01-01T01:00:00.000Z"},
        }

        with pytest.raises(NotionUncopyableBlockError, match=f"blk-5.*{kind}"):
            copyable_blocks(_lookup({}), [_fetched("blk-5", kind, payload)])
