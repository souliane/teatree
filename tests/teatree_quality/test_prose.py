"""Unit surface of the prose extractor: what a human wrote ABOUT the code."""

from pathlib import Path

from teatree.quality.prose import file_prose, markdown_prose, python_prose


class TestPythonProse:
    def test_docstrings_and_comments_are_prose(self) -> None:
        lines = python_prose('"""Doc line."""\n\n# a comment\nx = 1\n')
        assert [line.text for line in lines] == ["Doc line.", "# a comment"]

    def test_string_literals_are_not(self) -> None:
        assert python_prose('BODY = "TODO: replace with the rationale"\n') == []

    def test_a_nested_docstring_carries_its_own_line_numbers(self) -> None:
        lines = python_prose('def f() -> None:\n    """Inner."""\n')
        assert [(line.lineno, line.text) for line in lines] == [(2, "Inner.")]

    def test_unparseable_source_yields_nothing(self) -> None:
        assert python_prose("def (:\n") == []


class TestMarkdownProse:
    def test_fenced_blocks_are_skipped(self) -> None:
        source = "prose one\n```\nTODO: inside a fence\n```\nprose two\n"
        assert [line.text for line in markdown_prose(source)] == ["prose one", "prose two"]

    def test_lines_keep_their_file_positions(self) -> None:
        assert [line.lineno for line in markdown_prose("a\nb\nc\n")] == [1, 2, 3]


class TestFileProse:
    def test_link_targets_are_dropped(self, tmp_path: Path) -> None:
        # A URL's dots would otherwise end the sentence a pattern is scoped to.
        path = tmp_path / "doc.md"
        path.write_text("see [#2240](https://example.invalid/i/2240) for the rest.\n", encoding="utf-8")
        assert [line.text for line in file_prose(path)] == ["see [#2240] for the rest."]

    def test_suffix_picks_the_extractor(self, tmp_path: Path) -> None:
        module = tmp_path / "mod.py"
        module.write_text("# a comment\nx = 1\n", encoding="utf-8")
        assert [line.text for line in file_prose(module)] == ["# a comment"]
