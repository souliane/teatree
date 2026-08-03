"""A unified diff resolves to the skill SECTIONS it touched (#3944).

Drives the parser against REAL ``git diff --unified=0`` output over a real repo under
``tmp_path``, so the hunk-header format the mapping depends on is the one git actually
emits rather than a hand-rolled approximation. Hand-written diffs are used only for the
shapes git will not produce on demand (an unreadable post-image, a body line that looks
like a file header).

Every uncertainty must degrade to "no entry for this path", which the selector reads as
"granularity unknown → the whole file changed". A wrong answer there costs API budget; the
opposite error is the silent miss #3944 reports.
"""

import subprocess
from pathlib import Path

import pytest

from teatree.eval.changed_sections import changed_sections_by_path

SKILL = "skills/rules/SKILL.md"

BASELINE = """# Rules

Framing preamble.

## Alpha

Alpha body.

### Alpha Carve-Out

Carve-out body.

## Beta

Beta body.

## Gamma

Gamma body.
"""


def _git(repo: Path, *args: str) -> str:
    result = subprocess.run(
        ["git", *args],  # noqa: S607 — git resolved from PATH in test
        cwd=repo,
        check=True,
        capture_output=True,
        text=True,
    )
    return result.stdout


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    _git(tmp_path, "init", "-q", "-b", "main")
    _git(tmp_path, "config", "user.email", "t@t")
    _git(tmp_path, "config", "user.name", "t")
    skill = tmp_path / SKILL
    skill.parent.mkdir(parents=True)
    skill.write_text(BASELINE, encoding="utf-8")
    _git(tmp_path, "add", "-A")
    _git(tmp_path, "commit", "-qm", "base")
    return tmp_path


def _diff_after(repo: Path, text: str) -> str:
    (repo / SKILL).write_text(text, encoding="utf-8")
    return _git(repo, "diff", "--unified=0")


class TestSectionAttribution:
    def test_edit_inside_one_section_names_only_that_section(self, repo: Path) -> None:
        diff = _diff_after(repo, BASELINE.replace("Beta body.", "Beta body, revised."))
        assert changed_sections_by_path(diff, repo_root=repo) == {SKILL: frozenset({"Beta"})}

    def test_edit_in_a_subsection_names_the_parent_too(self, repo: Path) -> None:
        # ``extract_sections`` ends a section at the next SAME-OR-SHALLOWER heading, so the
        # carve-out is part of what a scenario naming "Alpha" is sent — both must select.
        diff = _diff_after(repo, BASELINE.replace("Carve-out body.", "Carve-out body, revised."))
        assert changed_sections_by_path(diff, repo_root=repo) == {SKILL: frozenset({"Alpha", "Alpha Carve-Out"})}

    def test_pure_deletion_names_the_section_it_was_removed_from(self, repo: Path) -> None:
        diff = _diff_after(repo, BASELINE.replace("Gamma body.\n", ""))
        assert changed_sections_by_path(diff, repo_root=repo) == {SKILL: frozenset({"Gamma"})}

    def test_removed_heading_is_harvested_from_the_diff_body(self, repo: Path) -> None:
        # The section no longer exists in the post-image, so line mapping alone cannot see it;
        # a scenario still naming it must be selected (it is about to fail loud at load time).
        diff = _diff_after(repo, BASELINE.replace("## Beta\n\nBeta body.\n\n", ""))
        assert changed_sections_by_path(diff, repo_root=repo)[SKILL] >= frozenset({"Beta"})

    def test_added_section_is_named(self, repo: Path) -> None:
        diff = _diff_after(repo, BASELINE + "## Delta\n\nDelta body.\n")
        assert changed_sections_by_path(diff, repo_root=repo) == {SKILL: frozenset({"Delta"})}


class TestFailSafeToWholeFile:
    def test_preamble_edit_yields_no_entry(self, repo: Path) -> None:
        # The preamble is prepended to EVERY section-scoped prompt, so no section is unaffected.
        diff = _diff_after(repo, BASELINE.replace("Framing preamble.", "Framing preamble, revised."))
        assert changed_sections_by_path(diff, repo_root=repo) == {}

    def test_deleted_file_yields_no_entry(self, repo: Path) -> None:
        (repo / SKILL).unlink()
        diff = _git(repo, "diff", "--unified=0")
        assert changed_sections_by_path(diff, repo_root=repo) == {}

    def test_post_image_missing_from_the_worktree_yields_no_entry(self, tmp_path: Path) -> None:
        diff = "--- a/x.md\n+++ b/x.md\n@@ -1,0 +2 @@\n+added\n"
        assert changed_sections_by_path(diff, repo_root=tmp_path) == {}

    def test_file_without_headings_yields_no_entry(self, repo: Path) -> None:
        plain = repo / "agents" / "planner.md"
        plain.parent.mkdir()
        plain.write_text("no headings here\n", encoding="utf-8")
        _git(repo, "add", "-A")
        _git(repo, "commit", "-qm", "plain")
        plain.write_text("no headings here, revised\n", encoding="utf-8")
        assert changed_sections_by_path(_git(repo, "diff", "--unified=0"), repo_root=repo) == {}

    def test_empty_diff_yields_no_entry(self, tmp_path: Path) -> None:
        assert changed_sections_by_path("", repo_root=tmp_path) == {}

    def test_combined_merge_hunk_header_yields_no_entry(self, repo: Path) -> None:
        # `git diff -c` emits `@@@ … @@@`, whose two old-file ranges the two-way regex
        # rejects; recording nothing is the fail-safe reading, never a wrong line number.
        diff = f"--- a/{SKILL}\n+++ b/{SKILL}\n@@@ -14,0 -14,0 +15 @@@\n++new\n"
        assert changed_sections_by_path(diff, repo_root=repo) == {}


class TestHunkBodyIsNotAFileHeader:
    def test_added_line_starting_like_a_file_header_does_not_retarget(self, repo: Path) -> None:
        # ``+++ b/other.md`` INSIDE a hunk body is added content, not the next file's header;
        # a naive prefix check would attribute the rest of the diff to ``other.md``.
        diff = f"--- a/{SKILL}\n+++ b/{SKILL}\n@@ -14,0 +15 @@\n++++ b/other.md\n@@ -18,0 +19 @@\n+trailing\n"
        assert changed_sections_by_path(diff, repo_root=repo) == {SKILL: frozenset({"Beta", "Gamma"})}
