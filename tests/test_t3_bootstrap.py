"""Tests for the ``t3_bootstrap`` entry-point launcher."""

import shutil
import subprocess
import sys
import zipfile
from pathlib import Path
from unittest.mock import patch

import pytest

import t3_bootstrap

_REPO_ROOT = Path(__file__).resolve().parent.parent


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


class TestWheelShipsBootstrap:
    """Regression guard for #397 / #498.

    The ``t3`` console script resolves ``t3_bootstrap:main`` at runtime, so a
    non-editable install (``uv tool install --from git+...teatree.git``) fails
    with ``ModuleNotFoundError: No module named 't3_bootstrap'`` unless the
    built wheel actually ships the ``t3_bootstrap`` package.  #434 declared it
    via a hatch ``force-include`` that the project's ``uv_build`` backend
    silently ignored, so every wheel since shipped without it and the failure
    surfaced only at install time in CI.  Asserting the ``pyproject.toml`` key
    alone would not catch a backend swap; only an actual build does.
    """

    @pytest.fixture(scope="class")
    def built_wheel(self, tmp_path_factory: pytest.TempPathFactory) -> Path:
        uv = shutil.which("uv")
        if uv is None:
            pytest.skip("uv not available — cannot build wheel")
        out_dir = tmp_path_factory.mktemp("wheel")
        subprocess.run(
            [uv, "build", "--wheel", "-o", str(out_dir)],
            cwd=_REPO_ROOT,
            check=True,
            capture_output=True,
        )
        wheels = list(out_dir.glob("*.whl"))
        assert len(wheels) == 1, f"expected exactly one wheel, got {wheels}"
        return wheels[0]

    def test_wheel_contains_t3_bootstrap_package(self, built_wheel: Path) -> None:
        with zipfile.ZipFile(built_wheel) as zf:
            names = set(zf.namelist())
        assert "t3_bootstrap/__init__.py" in names
        assert "t3_bootstrap/_main.py" in names

    def test_wheel_console_script_points_at_bootstrap(self, built_wheel: Path) -> None:
        with zipfile.ZipFile(built_wheel) as zf:
            entry_points = next(zf.read(n).decode() for n in zf.namelist() if n.endswith(".dist-info/entry_points.txt"))
        assert "t3 = t3_bootstrap:main" in entry_points
