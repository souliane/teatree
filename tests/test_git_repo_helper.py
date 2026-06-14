"""Tests for the shared default-branch-independent real-git fixture builder.

The contract that matters (souliane/teatree#2359): :func:`make_git_repo` bornes
the requested branch regardless of the host's ``init.defaultBranch``. The
``GIT_CONFIG_NOSYSTEM`` lane in CI (and ``tests/conftest.py`` preserving the var)
exercises these under git's compiled-in ``master`` default, so a repo built here
must carry ``main`` (or whatever was asked) even when the ambient default differs.
"""

from pathlib import Path

from tests._git_repo import make_git_repo, run_git


class TestMakeGitRepo:
    def test_default_branch_is_main_regardless_of_ambient_default(self, tmp_path: Path) -> None:
        repo = make_git_repo(tmp_path / "repo")
        assert run_git(repo, "symbolic-ref", "--short", "HEAD") == "main"

    def test_initial_commit_gives_main_a_resolvable_head(self, tmp_path: Path) -> None:
        repo = make_git_repo(tmp_path / "repo")
        # A ``worktree add <path> main`` resolves only if ``main`` has a HEAD; it
        # also needs ``main`` to not be the repo's own checkout, so move off it.
        run_git(repo, "checkout", "-q", "-b", "feature")
        run_git(repo, "worktree", "add", str(tmp_path / "wt"), "main")
        assert (tmp_path / "wt").is_dir()

    def test_custom_default_branch_is_borned(self, tmp_path: Path) -> None:
        repo = make_git_repo(tmp_path / "repo", default_branch="trunk")
        assert run_git(repo, "symbolic-ref", "--short", "HEAD") == "trunk"

    def test_without_initial_commit_branch_is_unborn(self, tmp_path: Path) -> None:
        repo = make_git_repo(tmp_path / "repo", initial_commit=False)
        # No commit yet: HEAD points at the symbolic ref but it has no object,
        # so ``rev-parse HEAD`` does not resolve.
        assert run_git(repo, "symbolic-ref", "--short", "HEAD") == "main"
        assert run_git(repo, "rev-parse", "--verify", "--quiet", "HEAD", check=False) == ""

    def test_bare_repo_has_no_working_tree(self, tmp_path: Path) -> None:
        repo = make_git_repo(tmp_path / "remote.git", bare=True)
        assert run_git(repo, "rev-parse", "--is-bare-repository") == "true"
        assert not (repo / ".git").exists()
