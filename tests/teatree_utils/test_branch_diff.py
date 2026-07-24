"""branch_diff measures the branch's committed diff vs base, never the worktree.

Regression for Gate 12: ``t3 tool diff-coverage`` shelled ``full_worktree_diff``,
so the PR-create gate measured the clone's *uncommitted* working tree instead of
the branch's committed ``<merge-base>..HEAD`` diff — denying legit PRs over
unrelated dirt and missing the very lines the PR adds (they live in HEAD, absent
from ``git diff HEAD``).
"""

import os
from pathlib import Path
from unittest.mock import patch

from teatree.cli.enforcement_tools import diff_coverage
from teatree.utils.git import branch_diff, full_worktree_diff, resolve_diff_base
from teatree.utils.git_run import run_strict


def _git(repo: Path, *args: str) -> None:
    run_strict(repo=str(repo), args=list(args))


def _added_lines(diff: str) -> str:
    """The diff's added lines only (so a context line is never mistaken for a change)."""
    return "\n".join(line for line in diff.splitlines() if line.startswith("+") and not line.startswith("+++"))


def _repo_with_committed_branch_and_worktree_dirt(tmp_path: Path) -> Path:
    repo = tmp_path / "clone"
    repo.mkdir()
    _git(repo, "init", "-q", "-b", "main")
    _git(repo, "config", "user.email", "t@example.com")
    _git(repo, "config", "user.name", "t")
    (repo / "mod.py").write_text("def base():\n    return 1\n")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "base")

    _git(repo, "checkout", "-q", "-b", "feature")
    (repo / "mod.py").write_text("def base():\n    return 1\n\n\ndef feature():\n    return 2\n")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "feat")

    # Unrelated working-tree dirt: an uncommitted edit + a brand-new untracked file.
    (repo / "mod.py").write_text(
        "def base():\n    return 1\n\n\ndef feature():\n    return 2\n\n\ndef dirty():\n    return 3\n"
    )
    (repo / "untracked.py").write_text("def untracked():\n    return 4\n")
    return repo


def test_branch_diff_is_committed_diff_vs_base_not_worktree(tmp_path: Path) -> None:
    repo = _repo_with_committed_branch_and_worktree_dirt(tmp_path)

    committed = _added_lines(branch_diff(str(repo), "main"))
    assert "def feature" in committed
    assert "def dirty" not in committed
    assert "def untracked" not in committed

    # Anti-vacuity: full_worktree_diff (the old source) adds the exact opposite —
    # it misses the committed line and surfaces the dirt — so a revert to worktree
    # diffing flips every assertion above. (def feature is only a context line in
    # the worktree diff, never an added one.)
    worktree = _added_lines(full_worktree_diff(str(repo)))
    assert "def feature" not in worktree
    assert "def dirty" in worktree
    assert "def untracked" in worktree


def test_resolve_diff_base_qualifies_the_configured_base() -> None:
    with patch.dict(os.environ, {"T3_DIFF_COVERAGE_BASE": "develop"}, clear=False):
        # a bare name is remote-qualified
        assert resolve_diff_base(".") == "origin/develop"
    with patch.dict(os.environ, {"T3_DIFF_COVERAGE_BASE": "origin/release"}, clear=False):
        # an already-qualified ref passes through untouched
        assert resolve_diff_base(".") == "origin/release"


def test_resolve_diff_base_falls_back_to_origin_main_when_default_is_unresolvable(tmp_path: Path) -> None:
    repo = tmp_path / "clone"
    repo.mkdir()
    _git(repo, "init", "-q", "-b", "main")
    with patch.dict(os.environ, {}, clear=False):
        os.environ.pop("T3_DIFF_COVERAGE_BASE", None)
        # no origin remote → default branch is unresolvable → last-resort fallback
        assert resolve_diff_base(str(repo)) == "origin/main"


def test_diff_coverage_cli_uses_branch_diff_not_worktree(tmp_path: Path) -> None:
    with (
        patch("teatree.utils.git.branch_diff", return_value="") as branch,
        patch("teatree.utils.git.full_worktree_diff") as worktree,
    ):
        diff_coverage(
            repo=tmp_path,
            base="origin/main",
            coverage_file=tmp_path / ".coverage",
            output_json=True,
        )

    branch.assert_called_once_with(str(tmp_path), "origin/main")
    worktree.assert_not_called()
