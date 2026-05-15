"""Tests for the worktree-first pre-commit guard (#638).

The hook refuses a commit when it runs in a *main clone* (``.git`` is a
directory) on a non-default branch — pushing developers into a worktree
instead of polluting the shared clone.
"""

import subprocess
from pathlib import Path

import pytest

HOOK = Path(__file__).resolve().parents[1] / "scripts" / "hooks" / "refuse-main-clone-commit.sh"


def _git(cwd: Path, *args: str) -> None:
    subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True)  # noqa: S607


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
        import os  # noqa: PLC0415

        assert os.access(HOOK, os.X_OK), f"{HOOK} must be chmod +x"


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-q"]))
