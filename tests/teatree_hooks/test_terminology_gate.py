r"""Tests for the built-in terminology gate.

The gate flags the two routinely-conflated phrases — ``teatree todo`` (no
such store exists) and ``Claude TODO`` (harness-specific where teatree
stays harness-agnostic) — and steers each to the right term: ``teatree
task`` for the DB ``Task`` model, ``harness TODO`` for the harness list.

The gate carries a carve-out so a line that documents the rule (it names
the correct term alongside the banned phrase) is not itself flagged.
"""

import os
import subprocess
from pathlib import Path

from teatree.hooks import banned_terms_tree_scan, terminology_gate

_CORRECTION = "use 'teatree task' (DB Task model) or 'harness TODO' (harness list)"


class TestScanLine:
    def test_flags_teatree_todo(self) -> None:
        findings = terminology_gate.scan_line("clear the teatree todo first")
        assert len(findings) == 1
        assert findings[0].phrase.lower() == "teatree todo"
        assert findings[0].correction == _CORRECTION

    def test_flags_teatree_todos_plural(self) -> None:
        findings = terminology_gate.scan_line("list the teatree todos")
        assert len(findings) == 1
        assert findings[0].phrase.lower() == "teatree todos"

    def test_flags_teatree_todo_hyphenated(self) -> None:
        assert terminology_gate.scan_line("the teatree-todo list") != []

    def test_flags_claude_todo(self) -> None:
        findings = terminology_gate.scan_line("merge the Claude TODO list")
        assert len(findings) == 1
        assert findings[0].phrase == "Claude TODO"
        assert findings[0].correction == _CORRECTION

    def test_flags_claude_code_todo(self) -> None:
        assert terminology_gate.scan_line("the Claude Code TODO items") != []

    def test_match_is_case_insensitive(self) -> None:
        assert terminology_gate.scan_line("TEATREE TODO") != []

    def test_allows_teatree_task(self) -> None:
        assert terminology_gate.scan_line("claim the next teatree task") == []

    def test_allows_harness_todo(self) -> None:
        assert terminology_gate.scan_line("read the harness TODO list") == []

    def test_carve_out_allows_a_line_that_documents_the_rule(self) -> None:
        # A line pairing the banned phrase with the corrected term is the
        # rule being documented, not a conflation.
        line = "there is no 'teatree todo' — use 'teatree task' or 'harness TODO'"
        assert terminology_gate.scan_line(line) == []

    def test_clean_line_is_empty(self) -> None:
        assert terminology_gate.scan_line("nothing to see here") == []


class TestScanText:
    def test_reports_line_numbers(self) -> None:
        text = "clean line\nthe teatree todo\nanother clean line\n"
        hits = terminology_gate.scan_text(text)
        assert [lineno for lineno, _ in hits] == [2]


class TestPathExemption:
    def test_gate_source_is_exempt(self) -> None:
        assert terminology_gate.path_is_exempt("src/teatree/hooks/terminology_gate.py")

    def test_gate_test_old_path_is_not_exempt(self) -> None:
        # The file moved; the old path must NOT be exempt so it cannot be used
        # as a bypass by files planted at the stale location.
        assert not terminology_gate.path_is_exempt("tests/test_terminology_gate.py")

    def test_gate_test_at_moved_path_is_exempt(self) -> None:
        # The test file moved to tests/teatree_hooks/ — the exempt suffix must
        # match the real location so the gate does not self-trip on its fixtures.
        assert terminology_gate.path_is_exempt("tests/teatree_hooks/test_terminology_gate.py")

    def test_other_path_is_not_exempt(self) -> None:
        assert not terminology_gate.path_is_exempt("skills/checking/SKILL.md")


def _git(cwd: Path, *args: str) -> None:
    subprocess.run(
        ["git", *args],  # noqa: S607
        cwd=cwd,
        check=True,
        capture_output=True,
        env={
            **os.environ,
            "GIT_CONFIG_GLOBAL": "/dev/null",
            "GIT_CONFIG_SYSTEM": "/dev/null",
            "GIT_AUTHOR_NAME": "t",
            "GIT_AUTHOR_EMAIL": "t@example.com",
            "GIT_COMMITTER_NAME": "t",
            "GIT_COMMITTER_EMAIL": "t@example.com",
        },
    )


def _repo_with(tmp_path: Path, relpath: str, content: str) -> Path:
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-b", "main")
    target = repo / relpath
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(content, encoding="utf-8")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-m", "seed")
    return repo


class TestTerminologyGateInTreeScan:
    """The terminology gate rides the full-tree scan, with no brands configured."""

    def test_tree_scan_catches_teatree_todo(self, tmp_path: Path) -> None:
        repo = _repo_with(tmp_path, "docs/notes.md", "Clear the teatree todo before shipping.\n")
        findings = banned_terms_tree_scan.scan_tree(repo, ())
        assert len(findings) == 1
        assert findings[0].path == "docs/notes.md"
        assert findings[0].lineno == 1
        assert "teatree task" in findings[0].term
        assert "harness TODO" in findings[0].term

    def test_tree_scan_allows_teatree_task_and_harness_todo(self, tmp_path: Path) -> None:
        repo = _repo_with(
            tmp_path,
            "docs/notes.md",
            "Claim the next teatree task.\nRead the harness TODO list.\n",
        )
        assert banned_terms_tree_scan.scan_tree(repo, ()) == []
