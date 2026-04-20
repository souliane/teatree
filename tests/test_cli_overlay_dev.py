from pathlib import Path

import pytest

import teatree.cli.overlay_dev
from teatree.cli.overlay_dev import OverlayDevError, _resolve_teatree_worktree, overlay_dev_app


class TestOverlayDevModule:
    def test_module_importable(self) -> None:
        assert teatree.cli.overlay_dev is not None

    def test_has_typer_app(self) -> None:
        assert overlay_dev_app is not None


class TestResolveTeatreeWorktree:
    def _make_worktree(self, path: Path) -> Path:
        path.mkdir(parents=True)
        (path / "pyproject.toml").write_text('[project]\nname = "teatree"\n')
        (path / ".git").write_text("gitdir: /fake\n")
        return path

    def test_returns_worktree_root_when_cwd_is_worktree(self, tmp_path: Path) -> None:
        worktree = self._make_worktree(tmp_path / "ac-teatree-120-xyz" / "teatree")

        assert _resolve_teatree_worktree(worktree) == worktree

    def test_walks_up_from_subdirectory(self, tmp_path: Path) -> None:
        worktree = self._make_worktree(tmp_path / "ac-teatree-120-xyz" / "teatree")
        (worktree / "src" / "teatree").mkdir(parents=True)

        assert _resolve_teatree_worktree(worktree / "src" / "teatree") == worktree

    def test_refuses_main_clone(self, tmp_path: Path) -> None:
        clone = tmp_path / "souliane" / "teatree"
        clone.mkdir(parents=True)
        (clone / "pyproject.toml").write_text('[project]\nname = "teatree"\n')
        (clone / ".git").mkdir()

        with pytest.raises(OverlayDevError, match="main clone"):
            _resolve_teatree_worktree(clone)

    def test_refuses_non_teatree_dir(self, tmp_path: Path) -> None:
        other = tmp_path / "other-repo"
        other.mkdir()
        (other / "pyproject.toml").write_text('[project]\nname = "other"\n')
        (other / ".git").write_text("gitdir: /fake\n")

        with pytest.raises(OverlayDevError, match="not a teatree"):
            _resolve_teatree_worktree(other)

    def test_raises_when_no_pyproject_found(self, tmp_path: Path) -> None:
        empty = tmp_path / "empty"
        empty.mkdir()

        with pytest.raises(OverlayDevError, match="No teatree worktree"):
            _resolve_teatree_worktree(empty)
