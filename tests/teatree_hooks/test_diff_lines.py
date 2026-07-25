"""Tests for the shared unified-diff added-line parser (``hooks/_diff_lines``).

The two privacy diff hooks route their added-line iteration through this
module. These cover the parsing primitives directly: the ``+`` vs ``+++`` edge
(a file header is never mistaken for an added line), multi-file hunks (each
added line carries its own file's path), the 1-based line-number contract, and
the header-regex tolerances (optional ``b/`` prefix, stripped tab metadata).
"""

from teatree.hooks._diff_lines import is_added_line, is_doc_path, iter_added_lines


def _diff(path: str, *added_lines: str) -> str:
    body = "".join(f"+{line}\n" for line in added_lines)
    return (
        f"diff --git a/{path} b/{path}\n"
        f"index 0000000..1111111 100644\n"
        f"--- a/{path}\n"
        f"+++ b/{path}\n"
        f"@@ -0,0 +1,{len(added_lines)} @@\n"
        f"{body}"
    )


class TestIsAddedLine:
    def test_added_body_line_is_added(self) -> None:
        assert is_added_line("+    x = 1")

    def test_file_header_is_not_added(self) -> None:
        # The `+++ b/<path>` header shares the `+` prefix but is not a body line.
        assert not is_added_line("+++ b/src/x.py")

    def test_context_and_removed_lines_are_not_added(self) -> None:
        assert not is_added_line("     context")
        assert not is_added_line("-    removed")


class TestIterAddedLines:
    def test_yields_only_added_bodies_not_the_header(self) -> None:
        diff = _diff("src/x.py", "a = 1", "b = 2")
        lines = list(iter_added_lines(diff))
        assert [ln.body for ln in lines] == ["a = 1", "b = 2"]
        # The `+++ b/src/x.py` header must never leak in as an added line.
        assert all(ln.body not in {"+ b/src/x.py", "src/x.py"} for ln in lines)

    def test_line_numbers_are_1_based_positions_in_the_text(self) -> None:
        diff = _diff("src/x.py", "a = 1", "b = 2")
        linenos = [ln.lineno for ln in iter_added_lines(diff)]
        # Header block is 5 lines; the two added lines are text lines 6 and 7.
        assert linenos == [6, 7]
        splat = diff.splitlines()
        for ln in iter_added_lines(diff):
            assert splat[ln.lineno - 1] == f"+{ln.body}"

    def test_multi_file_hunks_carry_per_file_paths(self) -> None:
        diff = _diff("src/a.py", "first") + _diff("src/b.ts", "second")
        by_body = {ln.body: ln.path for ln in iter_added_lines(diff)}
        assert by_body == {"first": "src/a.py", "second": "src/b.ts"}

    def test_added_line_before_any_header_is_skipped(self) -> None:
        # No `+++` header yet — a bare `+` line has no path context.
        assert list(iter_added_lines("+orphan added line\n")) == []

    def test_header_accepts_bare_path_and_strips_tab_metadata(self) -> None:
        text = "+++ src/plain.py\n+x = 1\n+++ b/tabbed.py\t2026-01-01 12:00:00\n+y = 2\n"
        paths = {ln.body: ln.path for ln in iter_added_lines(text)}
        assert paths == {"x = 1": "src/plain.py", "y = 2": "tabbed.py"}


class TestIsDocPath:
    def test_markdown_and_docs_dir_and_changelog_are_docs(self) -> None:
        assert is_doc_path("BLUEPRINT.md")
        assert is_doc_path("docs/x.py")
        assert is_doc_path("packages/docs/snippet.py")
        assert is_doc_path("CHANGELOG.rst")

    def test_source_file_is_not_docs(self) -> None:
        assert not is_doc_path("src/teatree/x.py")
