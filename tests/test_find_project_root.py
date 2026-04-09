"""Tests for teatree.find_project_root."""

from pathlib import Path
from unittest.mock import patch

import teatree


class TestFindProjectRoot:
    def test_finds_root_from_package(self) -> None:
        """find_project_root returns the actual teatree project root."""
        root = teatree.find_project_root()
        assert root is not None
        assert (root / "pyproject.toml").is_file()
        assert (root / ".git").exists()

    def test_returns_none_without_markers(self, tmp_path: Path) -> None:
        """Returns None when no ancestor has .git + pyproject.toml."""
        fake_init = tmp_path / "a" / "b" / "__init__.py"
        fake_init.parent.mkdir(parents=True)
        fake_init.touch()
        with patch.object(teatree, "__file__", str(fake_init)):
            assert teatree.find_project_root() is None

    def test_walks_up_to_correct_depth(self, tmp_path: Path) -> None:
        """Finds root regardless of how deeply nested the file is."""
        (tmp_path / ".git").mkdir()
        (tmp_path / "pyproject.toml").write_text("[project]\n")
        deep = tmp_path / "a" / "b" / "c" / "d" / "e" / "__init__.py"
        deep.parent.mkdir(parents=True)
        deep.touch()
        with patch.object(teatree, "__file__", str(deep)):
            assert teatree.find_project_root() == tmp_path
