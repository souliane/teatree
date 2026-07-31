"""Unit surface of the prose extractor: what a human wrote ABOUT the code."""

from pathlib import Path

import pytest

from teatree.quality.prose import (
    file_comments,
    file_prose,
    hash_comments,
    markdown_prose,
    python_comments,
    python_prose,
)


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


class TestPythonComments:
    def test_a_docstring_is_not_a_comment(self) -> None:
        lines = python_comments('"""Doc line."""\n\n# a comment\nx = 1\n')
        assert [line.text for line in lines] == ["# a comment"]

    def test_unparseable_source_yields_nothing(self) -> None:
        assert python_comments("x = '\n") == []


class TestHashComments:
    def test_a_trailing_comment_is_found_from_its_sigil(self) -> None:
        lines = hash_comments("name: probe  # a trailing note\n")
        assert [(line.lineno, line.text) for line in lines] == [(1, "# a trailing note")]

    def test_a_sigil_inside_a_quoted_scalar_is_data(self) -> None:
        assert hash_comments('prompt: "run gh issue view #12"\n') == []

    def test_a_comment_after_a_quoted_scalar_is_still_found(self) -> None:
        assert [line.text for line in hash_comments('prompt: "a value"  # the note\n')] == ["# the note"]


class TestFileComments:
    @pytest.mark.parametrize(
        ("name", "body", "expected"),
        [
            ("mod.py", '"""Doc."""\n# a note\n', ["# a note"]),
            ("conf.yaml", "# a note\nname: x\n", ["# a note"]),
            ("conf.toml", "# a note\nkey = 1\n", ["# a note"]),
            ("doc.md", "a line\n", ["a line"]),
        ],
    )
    def test_suffix_picks_the_extractor(self, tmp_path: Path, name: str, body: str, expected: list[str]) -> None:
        path = tmp_path / name
        path.write_text(body, encoding="utf-8")
        assert [line.text for line in file_comments(path)] == expected

    def test_a_format_without_comment_syntax_has_no_reader_line(self, tmp_path: Path) -> None:
        path = tmp_path / "fixture.jsonl"
        path.write_text('{"text": "# a value"}\n', encoding="utf-8")
        assert file_comments(path) == []


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
