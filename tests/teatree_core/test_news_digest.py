"""The press-review digest is Slack-formatted: clickable links, backticked symbols (#3669)."""

from teatree.core.news_digest import DigestItem, format_code_symbols, render_digest


class TestFormatCodeSymbols:
    def test_backticks_a_bare_file_path(self) -> None:
        assert format_code_symbols("see src/teatree/loop/run.py for it") == "see `src/teatree/loop/run.py` for it"

    def test_backticks_a_bare_dotted_symbol(self) -> None:
        assert format_code_symbols("call teatree.core.tasks.claim") == "call `teatree.core.tasks.claim`"

    def test_leaves_an_already_backticked_span_alone(self) -> None:
        assert format_code_symbols("use `src/a.py` here") == "use `src/a.py` here"

    def test_leaves_a_url_alone(self) -> None:
        text = "read https://example.com/a/b.py now"
        assert format_code_symbols(text) == text

    def test_leaves_ordinary_prose_alone(self) -> None:
        assert format_code_symbols("a sentence about agents.") == "a sentence about agents."


class TestRenderDigest:
    items = (
        DigestItem(
            title="An agent eval harness",
            url="https://example.com/eval",
            rationale="mirrors src/teatree/eval/backends.py",
        ),
        DigestItem(title="Loop scheduling", url="https://example.com/loop", rationale="relevant to the tick cadence"),
    )

    def test_every_item_is_a_clickable_slack_link(self) -> None:
        rendered = render_digest(self.items, scanned_on="2026-07-23")
        assert "<https://example.com/eval|An agent eval harness>" in rendered
        assert "<https://example.com/loop|Loop scheduling>" in rendered

    def test_no_markdown_link_syntax_survives_into_slack(self) -> None:
        assert "](" not in render_digest(self.items, scanned_on="2026-07-23")

    def test_a_file_path_in_a_rationale_is_backticked(self) -> None:
        assert "`src/teatree/eval/backends.py`" in render_digest(self.items, scanned_on="2026-07-23")

    def test_the_header_carries_the_scan_date_and_the_count(self) -> None:
        rendered = render_digest(self.items, scanned_on="2026-07-23")
        assert "2026-07-23" in rendered
        assert "2" in rendered.splitlines()[0]

    def test_an_empty_scan_renders_no_digest(self) -> None:
        assert render_digest((), scanned_on="2026-07-23") == ""
