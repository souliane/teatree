"""Tests for the worktree-first pre-commit guard (#638).

The hook refuses a commit when it runs in a *main clone* (``.git`` is a
directory) on a non-default branch — pushing developers into a worktree
instead of polluting the shared clone.
"""

import os
import subprocess
from pathlib import Path

import pytest

HOOK = Path(__file__).resolve().parents[1] / "scripts" / "hooks" / "refuse-main-clone-commit.sh"


def _hermetic_env() -> dict[str, str]:
    """Env with all GIT_* vars stripped.

    When the suite runs from the pre-commit ``pytest`` hook, the outer
    ``git commit`` exports ``GIT_DIR`` / ``GIT_INDEX_FILE`` /
    ``GIT_WORK_TREE``; inherited, they hijack the tmp-repo git calls
    here and ``git add`` targets the outer repo. Scrub them so every
    tmp-repo operation is hermetic regardless of caller context.
    """
    return {k: v for k, v in os.environ.items() if not k.startswith("GIT_")}


def _git(cwd: Path, *args: str) -> None:
    # S603/S607: trusted fixed argv (literal "git" + test-controlled flags),
    # no untrusted input — same justification as the repo's scripts/** ignore.
    subprocess.run(
        ["git", *args],  # noqa: S607
        cwd=cwd,
        check=True,
        capture_output=True,
        env=_hermetic_env(),
    )


def _make_main_clone(tmp_path: Path) -> Path:
    repo = tmp_path / "teatree"
    repo.mkdir()
    _git(repo, "init", "-b", "main")
    _git(repo, "config", "user.email", "t@e.st")
    _git(repo, "config", "user.name", "Tester")
    (repo / "f.txt").write_text("x", encoding="utf-8")
    _git(repo, "add", "f.txt")
    _git(repo, "commit", "-m", "init")
    # Give it an origin/HEAD so default-branch detection has something to read.
    _git(repo, "remote", "add", "origin", str(repo))
    _git(repo, "fetch", "origin")
    _git(repo, "symbolic-ref", "refs/remotes/origin/HEAD", "refs/remotes/origin/main")
    return repo


def _run_hook(cwd: Path) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["bash", str(HOOK)],  # noqa: S607
        cwd=cwd,
        capture_output=True,
        text=True,
        check=False,
        env=_hermetic_env(),
    )


class TestRefuseMainCloneCommit:
    def test_allows_commit_on_default_branch(self, tmp_path: Path) -> None:
        repo = _make_main_clone(tmp_path)
        result = _run_hook(repo)
        assert result.returncode == 0, result.stderr

    def test_refuses_commit_on_feature_branch_in_main_clone(self, tmp_path: Path) -> None:
        repo = _make_main_clone(tmp_path)
        _git(repo, "checkout", "-b", "ac/some-feature")

        result = _run_hook(repo)

        assert result.returncode == 1
        combined = result.stdout + result.stderr
        assert "worktree" in combined.lower()
        assert "t3" in combined

    def test_refuses_staged_tracked_edit_on_default_branch_in_main_clone(self, tmp_path: Path) -> None:
        """The #2614 gap: a tracked edit staged on the *default* branch slips the branch-only gate.

        The original incident — an agent ``git add``-staged a hand-edit directly
        in the managed main clone while it was still on ``main``. The branch-only
        gate exits 0 because the branch matches the default, so the staged edit
        commits into the shared clone. The worktree-first invariant must cover the
        staged state, not just the commit-on-a-feature-branch transition.
        """
        repo = _make_main_clone(tmp_path)
        (repo / "f.txt").write_text("staged-edit", encoding="utf-8")
        _git(repo, "add", "f.txt")

        result = _run_hook(repo)

        assert result.returncode == 1
        combined = result.stdout + result.stderr
        assert "worktree" in combined.lower()

    def test_allows_default_branch_in_main_clone_with_no_staged_changes(self, tmp_path: Path) -> None:
        """Clean default-branch state stays allowed — only a *staged tracked* change refuses.

        A fast-forward ``git pull`` of the managed main clone (the merge-keeping
        flow) stages nothing, so the gate must not fire on a clean default-branch
        tree. Guards against the staged-edit gate over-blocking the legit path.
        """
        repo = _make_main_clone(tmp_path)
        result = _run_hook(repo)
        assert result.returncode == 0, result.stdout + result.stderr

    def test_allows_staged_tracked_edit_in_a_worktree(self, tmp_path: Path) -> None:
        """A staged tracked edit in a *worktree* is fine — the gate is main-clone-only."""
        repo = _make_main_clone(tmp_path)
        wt = tmp_path / "wt"
        _git(repo, "worktree", "add", "-b", "ac/staged-feature", str(wt))
        (wt / "f.txt").write_text("worktree-staged-edit", encoding="utf-8")
        _git(wt, "add", "f.txt")

        result = _run_hook(wt)

        assert result.returncode == 0, result.stdout + result.stderr

    def test_refuses_from_a_subdirectory_of_the_main_clone(self, tmp_path: Path) -> None:
        """Refuse from a subdirectory of the main clone.

        The load-bearing case: cwd is a subdir, so `git rev-parse
        --git-dir` returns an *absolute* path and the cd-resolution must
        still equate it with `<toplevel>/.git`.
        """
        repo = _make_main_clone(tmp_path)
        _git(repo, "checkout", "-b", "ac/sub")
        subdir = repo / "scripts"
        subdir.mkdir()

        result = _run_hook(subdir)

        assert result.returncode == 1
        assert "worktree" in (result.stdout + result.stderr).lower()

    def test_allows_commit_in_a_worktree_on_feature_branch(self, tmp_path: Path) -> None:
        repo = _make_main_clone(tmp_path)
        wt = tmp_path / "wt"
        _git(repo, "worktree", "add", "-b", "ac/feature", str(wt))

        result = _run_hook(wt)

        assert result.returncode == 0, result.stdout + result.stderr

    def test_detects_development_default_branch(self, tmp_path: Path) -> None:
        """An overlay whose default branch is `development` is honoured."""
        repo = tmp_path / "overlay"
        repo.mkdir()
        _git(repo, "init", "-b", "development")
        _git(repo, "config", "user.email", "t@e.st")
        _git(repo, "config", "user.name", "Tester")
        (repo / "f.txt").write_text("x", encoding="utf-8")
        _git(repo, "add", "f.txt")
        _git(repo, "commit", "-m", "init")
        _git(repo, "remote", "add", "origin", str(repo))
        _git(repo, "fetch", "origin")
        _git(repo, "symbolic-ref", "refs/remotes/origin/HEAD", "refs/remotes/origin/development")

        on_default = _run_hook(repo)
        assert on_default.returncode == 0, on_default.stderr

        _git(repo, "checkout", "-b", "ac/x")
        on_feature = _run_hook(repo)
        assert on_feature.returncode == 1

    def test_falls_back_to_main_when_no_origin_head(self, tmp_path: Path) -> None:
        repo = tmp_path / "noorigin"
        repo.mkdir()
        _git(repo, "init", "-b", "main")
        _git(repo, "config", "user.email", "t@e.st")
        _git(repo, "config", "user.name", "Tester")
        (repo / "f.txt").write_text("x", encoding="utf-8")
        _git(repo, "add", "f.txt")
        _git(repo, "commit", "-m", "init")

        assert _run_hook(repo).returncode == 0
        _git(repo, "checkout", "-b", "feature")
        assert _run_hook(repo).returncode == 1

    def test_hook_is_executable(self) -> None:
        assert os.access(HOOK, os.X_OK), f"{HOOK} must be chmod +x"


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-q"]))
