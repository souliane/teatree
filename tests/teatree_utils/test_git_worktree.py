"""Generic worktree mechanics.

Canonical-repo resolution, structured porcelain parse + by-branch lookup, and
the checkout / hollow-directory guard. Exercised against real ``git`` under
``tmp_path`` — the parse and the ``.git``-is-a-file distinction are exactly the
details a mock would paper over.
"""

from pathlib import Path

import pytest

from teatree.utils.git_worktree import locked_worktree_paths
from teatree.utils.git_worktree_query import (
    WorktreeRecord,
    canonical_repo_root,
    git_common_dir,
    is_git_checkout,
    list_worktrees,
    worktree_for_branch,
)
from teatree.utils.run import run_checked


def _git(cwd: Path, *args: str) -> None:
    run_checked(
        ["git", "-c", "user.email=t@t.test", "-c", "user.name=t", "-c", "commit.gpgsign=false", *args],
        cwd=cwd,
    )


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    """A clone on branch ``main`` with one commit, at ``<tmp>/clonedir``."""
    root = tmp_path / "clonedir"
    root.mkdir()
    _git(root, "init", "-b", "main")
    (root / "file.txt").write_text("hello", encoding="utf-8")
    _git(root, "add", ".")
    _git(root, "commit", "-m", "initial")
    return root


class TestGitCommonDirAndCanonicalRoot:
    def test_common_dir_resolves_to_shared_git(self, repo: Path) -> None:
        common = git_common_dir(repo)
        assert common == (repo / ".git").resolve()

    def test_common_dir_from_a_linked_worktree_points_at_the_clone(self, repo: Path, tmp_path: Path) -> None:
        wt = tmp_path / "branch-named-dir"
        _git(repo, "worktree", "add", "-b", "feature", str(wt))
        # The worktree dir basename is NOT the repo name, yet the common dir
        # (and thus the canonical root) still resolves back to the clone.
        assert git_common_dir(wt) == (repo / ".git").resolve()
        assert canonical_repo_root(wt) == repo.resolve()

    def test_canonical_root_is_the_clone_for_the_clone_itself(self, repo: Path) -> None:
        assert canonical_repo_root(repo) == repo.resolve()

    def test_returns_none_for_non_git_path(self, tmp_path: Path) -> None:
        plain = tmp_path / "not-a-repo"
        plain.mkdir()
        assert git_common_dir(plain) is None
        assert canonical_repo_root(plain) is None


class TestIsGitCheckout:
    def test_true_for_a_clone(self, repo: Path) -> None:
        assert is_git_checkout(repo) is True

    def test_true_for_a_linked_worktree_where_git_is_a_file(self, repo: Path, tmp_path: Path) -> None:
        wt = tmp_path / "wt"
        _git(repo, "worktree", "add", str(wt))
        assert (wt / ".git").is_file()  # a gitdir pointer, not a directory
        assert is_git_checkout(wt) is True

    def test_false_for_a_hollow_directory(self, tmp_path: Path) -> None:
        hollow = tmp_path / "hollow"
        (hollow / "staticfiles").mkdir(parents=True)  # generated artifacts, no .git
        assert is_git_checkout(hollow) is False


class TestListWorktrees:
    def test_parses_the_primary_worktree(self, repo: Path) -> None:
        records = list_worktrees(str(repo))
        assert len(records) == 1
        record = records[0]
        assert record.path == repo.resolve()
        assert record.branch == "main"
        assert record.detached is False
        assert len(record.head) == 40  # a full SHA

    def test_parses_a_linked_branch_worktree(self, repo: Path, tmp_path: Path) -> None:
        wt = tmp_path / "feat"
        _git(repo, "worktree", "add", "-b", "feature", str(wt))
        by_branch = {r.branch: r for r in list_worktrees(str(repo))}
        assert set(by_branch) == {"main", "feature"}
        assert by_branch["feature"].path == wt.resolve()

    def test_detached_worktree_has_empty_branch(self, repo: Path, tmp_path: Path) -> None:
        head = run_checked(["git", "rev-parse", "HEAD"], cwd=repo).stdout.strip()
        wt = tmp_path / "detached"
        _git(repo, "worktree", "add", "--detach", str(wt), head)
        record = next(r for r in list_worktrees(str(repo)) if r.path == wt.resolve())
        assert record.detached is True
        assert record.branch == ""

    def test_locked_flag_is_parsed(self, repo: Path, tmp_path: Path) -> None:
        wt = tmp_path / "locked"
        _git(repo, "worktree", "add", str(wt))
        _git(repo, "worktree", "lock", str(wt))
        record = next(r for r in list_worktrees(str(repo)) if r.path == wt.resolve())
        assert record.locked is True

    def test_non_git_dir_yields_no_records(self, tmp_path: Path) -> None:
        plain = tmp_path / "plain"
        plain.mkdir()
        assert list_worktrees(str(plain)) == []


class TestWorktreeForBranch:
    def test_finds_the_worktree_holding_a_branch(self, repo: Path, tmp_path: Path) -> None:
        wt = tmp_path / "feat"
        _git(repo, "worktree", "add", "-b", "feature", str(wt))
        record = worktree_for_branch(str(repo), "feature")
        assert isinstance(record, WorktreeRecord)
        assert record.path == wt.resolve()

    def test_accepts_a_fully_qualified_ref(self, repo: Path) -> None:
        record = worktree_for_branch(str(repo), "refs/heads/main")
        assert record is not None
        assert record.branch == "main"

    def test_returns_none_when_no_worktree_holds_the_branch(self, repo: Path) -> None:
        assert worktree_for_branch(str(repo), "nonexistent") is None


class TestLockedWorktreePaths:
    def test_derives_locked_paths_from_the_shared_parse(self, repo: Path, tmp_path: Path) -> None:
        wt = tmp_path / "locked"
        _git(repo, "worktree", "add", str(wt))
        _git(repo, "worktree", "lock", str(wt))
        assert locked_worktree_paths(str(repo)) == {str(wt.resolve())}

    def test_empty_when_nothing_locked(self, repo: Path) -> None:
        assert locked_worktree_paths(str(repo)) == set()
