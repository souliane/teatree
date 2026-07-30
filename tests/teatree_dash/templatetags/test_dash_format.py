"""The dashboard's code-span filter — monospace symbols and paths, escaped first (#3624)."""

from teatree.dash.templatetags.dash_format import code_spans


class TestCodeSpansFilter:
    def test_wraps_a_file_path_in_a_code_element(self) -> None:
        assert code_spans("see src/teatree/loop/run.py") == "see <code>src/teatree/loop/run.py</code>"

    def test_wraps_a_dotted_symbol(self) -> None:
        assert code_spans("call teatree.core.tasks.claim") == "call <code>teatree.core.tasks.claim</code>"

    def test_escapes_html_before_wrapping(self) -> None:
        assert "<script>" not in code_spans("<script>alert(1)</script> src/a.py")

    def test_leaves_prose_alone(self) -> None:
        assert code_spans("a plain sentence") == "a plain sentence"

    def test_an_empty_value_renders_empty(self) -> None:
        assert code_spans("") == ""
