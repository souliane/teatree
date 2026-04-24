"""Tests for the ``t3_bootstrap`` entry-point launcher."""

import sys
from pathlib import Path
from unittest.mock import patch

import pytest

import t3_bootstrap


def _make_teatree_tree(root: Path) -> Path:
    """Create a minimal pyproject + ``src/teatree/__init__.py`` layout under *root*."""
    (root / "pyproject.toml").write_text('[project]\nname = "teatree"\n')
    pkg = root / "src" / "teatree"
    pkg.mkdir(parents=True)
    (pkg / "__init__.py").touch()
    return root / "src"


class TestFindTeatreeSource:
    def test_returns_src_when_cwd_is_worktree_root(self, tmp_path: Path) -> None:
        expected = _make_teatree_tree(tmp_path)
        assert t3_bootstrap._find_teatree_source(tmp_path) == expected

    def test_walks_up_to_find_worktree_root(self, tmp_path: Path) -> None:
        expected = _make_teatree_tree(tmp_path)
        nested = tmp_path / "src" / "teatree" / "cli"
        nested.mkdir(parents=True, exist_ok=True)
        assert t3_bootstrap._find_teatree_source(nested) == expected

    def test_returns_none_when_no_teatree_pyproject(self, tmp_path: Path) -> None:
        (tmp_path / "pyproject.toml").write_text('[project]\nname = "other"\n')
        assert t3_bootstrap._find_teatree_source(tmp_path) is None

    def test_returns_none_when_pyproject_matches_but_src_layout_missing(self, tmp_path: Path) -> None:
        (tmp_path / "pyproject.toml").write_text('[project]\nname = "teatree"\n')
        assert t3_bootstrap._find_teatree_source(tmp_path) is None

    def test_returns_none_when_no_pyproject_in_ancestors(self, tmp_path: Path) -> None:
        nested = tmp_path / "a" / "b"
        nested.mkdir(parents=True)
        assert t3_bootstrap._find_teatree_source(nested) is None


class TestMain:
    def test_inserts_worktree_source_on_sys_path(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        worktree_src = _make_teatree_tree(tmp_path)
        monkeypatch.chdir(tmp_path)

        with patch.object(sys, "path", list(sys.path)) as _, patch("teatree.cli.main") as mock_main:
            t3_bootstrap.main()
            assert sys.path[0] == str(worktree_src)
            mock_main.assert_called_once_with()

    def test_leaves_sys_path_untouched_outside_worktree(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.chdir(tmp_path)
        before = list(sys.path)

        with patch("teatree.cli.main") as mock_main:
            t3_bootstrap.main()
            assert sys.path == before
            mock_main.assert_called_once_with()
