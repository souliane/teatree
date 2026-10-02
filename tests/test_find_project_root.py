"""Tests for teatree.find_project_root."""

import subprocess
from pathlib import Path
from unittest.mock import patch

import teatree.project
from teatree import find_project_root


def _git(repo: Path, *args: str) -> None:
    # Trusted fixed argv (literal "git" + test-controlled flags).
    subprocess.run(["git", "-C", str(repo), *args], check=True, capture_output=True)  # noqa: S607


def _make_clone_with_worktree(tmp_path: Path) -> tuple[Path, Path]:
    """Create a real primary clone and a linked git worktree under *tmp_path*.

    Returns ``(main_clone, worktree)``. The worktree's ``.git`` is a real
    ``gitdir:`` pointer file, so resolution back to the main clone exercises
    the same path the installed ``t3`` hits from a worktree checkout.
    """
    main_clone = tmp_path / "teatree"
    main_clone.mkdir()
    _git(main_clone, "init", "-b", "main")
    _git(main_clone, "config", "user.email", "t@example.com")
    _git(main_clone, "config", "user.name", "t")
    (main_clone / "pyproject.toml").write_text('[project]\nname = "teatree"\n')
    _git(main_clone, "add", "pyproject.toml")
    _git(main_clone, "commit", "-m", "init")
    worktree = tmp_path / "wt"
    _git(main_clone, "worktree", "add", str(worktree), "-b", "feature")
    return main_clone, worktree


class TestFindProjectRoot:
    def test_finds_root_from_package(self) -> None:
        """find_project_root returns the actual teatree project root."""
        root = find_project_root()
        assert root is not None
        assert (root / "pyproject.toml").is_file()
        assert (root / ".git").exists()

    def test_returns_none_without_markers(self, tmp_path: Path) -> None:
        """Returns None when no ancestor has .git + pyproject.toml."""
        fake_init = tmp_path / "a" / "b" / "__init__.py"
        fake_init.parent.mkdir(parents=True)
        fake_init.touch()
        with patch.object(teatree.project, "__file__", str(fake_init)):
            assert find_project_root() is None

    def test_walks_up_to_correct_depth(self, tmp_path: Path) -> None:
        """Finds root regardless of how deeply nested the file is."""
        (tmp_path / ".git").mkdir()
        (tmp_path / "pyproject.toml").write_text("[project]\n")
        deep = tmp_path / "a" / "b" / "c" / "d" / "e" / "__init__.py"
        deep.parent.mkdir(parents=True)
        deep.touch()
        with patch.object(teatree.project, "__file__", str(deep)):
            assert find_project_root() == tmp_path

    def test_resolves_worktree_to_main_clone(self, tmp_path: Path) -> None:
        """From a worktree checkout, find_project_root returns the main clone.

        A worktree's ``.git`` is a *file* (a ``gitdir:`` pointer), not a dir.
        The naive ``.exists()`` check matched the worktree itself and bound
        skills / the editable repo to the worktree, isolating the DB (#1507).
        """
        main_clone, worktree = _make_clone_with_worktree(tmp_path)
        pkg_file = worktree / "src" / "teatree" / "project.py"
        pkg_file.parent.mkdir(parents=True)
        pkg_file.touch()
        with patch.object(teatree.project, "__file__", str(pkg_file)):
            assert find_project_root() == main_clone

    def test_resolves_relative_gitdir_pointer(self, tmp_path: Path) -> None:
        """A relative ``gitdir:`` pointer resolves against the .git file's dir.

        ``git worktree add --relative-paths`` (and older git) writes a relative
        pointer; resolving it against the process cwd returns the wrong clone
        (or None → fallback to the worktree, reintroducing the bad anchor).
        """
        main_clone = tmp_path / "teatree"
        (main_clone / ".git" / "worktrees" / "wt").mkdir(parents=True)
        (main_clone / "pyproject.toml").write_text("[project]\n")
        worktree = tmp_path / "wt"
        (worktree / "src" / "teatree").mkdir(parents=True)
        (worktree / "pyproject.toml").write_text("[project]\n")
        # Relative pointer, as `git worktree add --relative-paths` writes it.
        (worktree / ".git").write_text("gitdir: ../teatree/.git/worktrees/wt\n")
        pkg_file = worktree / "src" / "teatree" / "project.py"
        pkg_file.touch()
        with patch.object(teatree.project, "__file__", str(pkg_file)):
            assert find_project_root() == main_clone
