"""Tests for the canonical inline ``#N (short title)`` reference renderer (#2092).

The user directive: every id-listing surface renders the title inline, never a
bare ``#N`` nor a link-only id. These tests pin the single chokepoint helper
both ``/checking`` and ``/todos`` consume.
"""

from teatree.core.ref_render import render_ref, short_title


class TestShortTitle:
    def test_empty_input_is_empty(self) -> None:
        assert short_title("") == ""
        assert short_title("   ") == ""

    def test_conventional_commit_prefix_is_dropped(self) -> None:
        assert short_title("fix(loop): widget falls over") == "widget falls over"
        assert short_title("feat: add the thing") == "add the thing"

    def test_word_budget_capped(self) -> None:
        topic = short_title("one two three four five six seven eight")
        assert topic == "one two three four five six"

    def test_long_topic_truncated_with_ellipsis(self) -> None:
        topic = short_title("supercalifragilistic expialidocious antidisestablishmentarianism")
        assert topic.endswith("…")
        assert len(topic) <= 48

    def test_prefix_only_falls_back_to_raw(self) -> None:
        # A title that is only a conventional-commit prefix keeps the raw text
        # rather than collapsing to empty.
        assert short_title("fix:") == "fix:"


class TestRenderRef:
    def test_bare_id_with_title_renders_inline(self) -> None:
        # The load-bearing assertion: the title text is present inline next to
        # the id. Asserting a bare ``#42`` with no title would be RED.
        rendered = render_ref("#42", title="fix the broken widget")
        assert rendered == "#42 (fix the broken widget)"
        assert "fix the broken widget" in rendered

    def test_bare_id_without_title_has_no_empty_parens(self) -> None:
        assert render_ref("#42", title="") == "#42"
        assert render_ref("#42") == "#42"

    def test_url_wraps_whole_label_and_title(self) -> None:
        rendered = render_ref("#42", title="fix the widget", url="https://h/42")
        # Title is INSIDE the clickable text, not a bare number beside an
        # unlinked title.
        assert rendered == "[#42 (fix the widget)](https://h/42)"

    def test_url_without_title_still_clickable(self) -> None:
        assert render_ref("#42", url="https://h/42") == "[#42](https://h/42)"

    def test_todo_namespace_label_carries_title(self) -> None:
        rendered = render_ref("TODO-7", title="land the regression eval")
        assert rendered == "TODO-7 (land the regression eval)"
