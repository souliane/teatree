"""Tests for ``find_main_clone`` — t3 setup main-clone resolution.

Lifted verbatim from the former monolithic ``tests/test_cli_setup.py``
(souliane/teatree#443). No behavior change: same assertions and helpers,
only relocated under a focused package by concern.
"""

import shutil
from pathlib import Path
from unittest.mock import patch

import pytest

from teatree.cli.setup.clone import find_main_clone

GIT_BIN = shutil.which("git") or "git"


def _init_git_repo(path: Path) -> None:
    """Initialize a minimal git repo with an empty commit so `git worktree add` works."""
    import subprocess  # noqa: PLC0415

    path.mkdir(parents=True, exist_ok=True)
    env = {"GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t", "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@t"}
    subprocess.run([GIT_BIN, "init", "-q", "-b", "main"], cwd=path, check=True)
    subprocess.run([GIT_BIN, "commit", "-q", "--allow-empty", "-m", "init"], cwd=path, check=True, env=env)


class TestFindMainClone:
    def test_returns_none_when_no_repo(self) -> None:
        with patch("teatree.cli.setup.clone.DoctorService") as mock_svc:
            mock_svc.find_teatree_repo.return_value = None
            assert find_main_clone() is None

    def test_resolves_worktree_to_main_clone(self, tmp_path: Path) -> None:
        import subprocess  # noqa: PLC0415

        main_clone = tmp_path / "teatree"
        _init_git_repo(main_clone)
        worktree = tmp_path / "wt"
        subprocess.run(
            [GIT_BIN, "worktree", "add", "-q", "-b", "feature", str(worktree)],
            cwd=main_clone,
            check=True,
        )
        with patch("teatree.cli.setup.clone.DoctorService") as mock_svc:
            mock_svc.find_teatree_repo.return_value = worktree
            assert find_main_clone() == main_clone

    def test_returns_repo_when_main_clone(self, tmp_path: Path) -> None:
        repo = tmp_path / "teatree"
        repo.mkdir()
        (repo / ".git").mkdir()
        with patch("teatree.cli.setup.clone.DoctorService") as mock_svc:
            mock_svc.find_teatree_repo.return_value = repo
            result = find_main_clone()
            assert result == repo

    def test_returns_none_when_git_file_unparseable(self, tmp_path: Path) -> None:
        repo = tmp_path / "teatree"
        repo.mkdir()
        (repo / ".git").write_text("not a gitdir line\n")
        with patch("teatree.cli.setup.clone.DoctorService") as mock_svc:
            mock_svc.find_teatree_repo.return_value = repo
            assert find_main_clone() is None

    def test_env_var_wins_over_cwd_heuristic(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        """``T3_REPO`` env var must take priority so setup from a worktree still targets the configured main clone."""
        main_clone = tmp_path / "main-clone"
        main_clone.mkdir()
        (main_clone / ".git").mkdir()
        (main_clone / "pyproject.toml").touch()
        monkeypatch.setenv("T3_REPO", str(main_clone))

        with patch("teatree.cli.setup.clone.DoctorService") as mock_svc:
            mock_svc.find_teatree_repo.return_value = tmp_path / "some-worktree"
            assert find_main_clone() == main_clone
            mock_svc.find_teatree_repo.assert_not_called()
