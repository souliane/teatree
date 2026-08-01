"""Rendering a fetched block tree back to readable Markdown."""

from typing import Any

from teatree.backends.notion.markdown import BlockMarkdownRenderer, rich_text_plain


def _span(text: str, **annotations: bool) -> dict[str, Any]:
    return {"plain_text": text, "annotations": annotations}


def _block(block_type: str, text: str, **payload: Any) -> dict[str, Any]:
    return {"id": f"b-{text[:4]}", "type": block_type, block_type: {"rich_text": [_span(text)], **payload}}


def _render(blocks: list[dict[str, Any]], children: dict[str, list[dict[str, Any]]] | None = None) -> str:
    lookup = children or {}
    return BlockMarkdownRenderer(lambda block_id: lookup.get(block_id, [])).render(blocks)


class TestRendering:
    def test_headings_and_prose_round_trip_to_readable_markdown(self) -> None:
        rendered = _render([_block("heading_2", "Requirements"), _block("paragraph", "The loan must price.")])

        assert rendered.splitlines()[0] == "## Requirements"
        assert "The loan must price." in rendered

    def test_lists_number_themselves_and_reset_between_runs(self) -> None:
        rendered = _render(
            [
                _block("numbered_list_item", "first"),
                _block("numbered_list_item", "second"),
                _block("paragraph", "break"),
                _block("numbered_list_item", "restarted"),
            ]
        )

        assert "1. first" in rendered
        assert "2. second" in rendered
        assert "1. restarted" in rendered

    def test_annotations_and_links_survive_as_inline_markdown(self) -> None:
        block = {
            "id": "b-1",
            "type": "paragraph",
            "paragraph": {
                "rich_text": [
                    _span("see ", bold=False),
                    {"plain_text": "MR !1", "annotations": {"bold": True}, "href": "https://example.test/1"},
                ]
            },
        }

        assert _render([block]).strip() == "see [**MR !1**](https://example.test/1)"

    def test_a_toggles_children_are_rendered_indented_beneath_it(self) -> None:
        toggle = {**_block("heading_2", "Notes", is_toggleable=True), "has_children": True}
        rendered = _render([toggle], {"b-Note": [_block("paragraph", "nested detail")]})

        assert "## Notes" in rendered
        assert "  nested detail" in rendered

    def test_a_to_do_shows_its_checkbox_state(self) -> None:
        rendered = _render([_block("to_do", "ship it", checked=True), _block("to_do", "verify", checked=False)])

        assert "- [x] ship it" in rendered
        assert "- [ ] verify" in rendered

    def test_an_unknown_block_type_is_labelled_not_silently_dropped(self) -> None:
        rendered = _render([_block("equation", "E=mc^2")])

        assert "<equation> E=mc^2" in rendered


class TestPlainText:
    def test_annotations_are_dropped_for_the_matching_key(self) -> None:
        assert rich_text_plain([_span("🔧 notes", bold=True), _span(" more")]) == "🔧 notes more"

    def test_an_empty_array_is_the_empty_string(self) -> None:
        assert rich_text_plain([]) == ""
