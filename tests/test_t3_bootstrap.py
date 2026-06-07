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


class TestResolvePinnedSource:
    def test_returns_t3_repo_src_when_env_set(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        expected = _make_teatree_tree(tmp_path)
        monkeypatch.setenv("T3_REPO", str(tmp_path))
        assert t3_bootstrap._resolve_pinned_source() == expected

    def test_expands_user_in_t3_repo(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        expected = _make_teatree_tree(tmp_path)
        monkeypatch.setenv("HOME", str(tmp_path.parent))
        monkeypatch.setenv("T3_REPO", str(Path("~") / tmp_path.name))
        assert t3_bootstrap._resolve_pinned_source() == expected

    def test_returns_none_when_env_unset(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("T3_REPO", raising=False)
        assert t3_bootstrap._resolve_pinned_source() is None

    def test_returns_none_when_t3_repo_blank(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("T3_REPO", "")
        assert t3_bootstrap._resolve_pinned_source() is None

    def test_returns_none_when_t3_repo_not_a_teatree_tree(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        (tmp_path / "pyproject.toml").write_text('[project]\nname = "other"\n')
        monkeypatch.setenv("T3_REPO", str(tmp_path))
        assert t3_bootstrap._resolve_pinned_source() is None

    def test_returns_none_when_t3_repo_missing_src_layout(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        (tmp_path / "pyproject.toml").write_text('[project]\nname = "teatree"\n')
        monkeypatch.setenv("T3_REPO", str(tmp_path))
        assert t3_bootstrap._resolve_pinned_source() is None

    def test_returns_none_when_t3_repo_points_at_missing_dir(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("T3_REPO", str(tmp_path / "does-not-exist"))
        assert t3_bootstrap._resolve_pinned_source() is None


class TestMain:
    def test_pins_to_t3_repo_when_env_set(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        repo_src = _make_teatree_tree(tmp_path)
        monkeypatch.setenv("T3_REPO", str(tmp_path))

        with patch.object(sys, "path", list(sys.path)), patch("teatree.cli.main") as mock_main:
            t3_bootstrap.main()
            assert sys.path[0] == str(repo_src)
            mock_main.assert_called_once_with()

    def test_ignores_cwd_worktree_when_env_unset(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        """#2055: a stray cwd inside a sibling teatree worktree must not inject its ``src``."""
        sibling_worktree = tmp_path / "ac" / "some-branch" / "teatree"
        sibling_worktree.mkdir(parents=True)
        worktree_src = _make_teatree_tree(sibling_worktree)
        monkeypatch.delenv("T3_REPO", raising=False)
        monkeypatch.chdir(sibling_worktree)
        before = list(sys.path)

        with patch("teatree.cli.main") as mock_main:
            t3_bootstrap.main()
            assert str(worktree_src) not in sys.path
            assert sys.path == before
            mock_main.assert_called_once_with()

    def test_pins_to_t3_repo_even_when_cwd_is_a_different_worktree(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """#2055: with ``T3_REPO`` set, a cwd inside an unrelated worktree never wins."""
        configured = tmp_path / "main-clone"
        configured.mkdir()
        configured_src = _make_teatree_tree(configured)

        sibling_worktree = tmp_path / "ac" / "branch" / "teatree"
        sibling_worktree.mkdir(parents=True)
        worktree_src = _make_teatree_tree(sibling_worktree)

        monkeypatch.setenv("T3_REPO", str(configured))
        monkeypatch.chdir(sibling_worktree)

        with patch.object(sys, "path", list(sys.path)), patch("teatree.cli.main") as mock_main:
            t3_bootstrap.main()
            assert sys.path[0] == str(configured_src)
            assert str(worktree_src) not in sys.path
            mock_main.assert_called_once_with()


class TestEndToEndExecTreePin:
    """#2055 end-to-end: run the real bootstrap from a sibling teatree worktree.

    The bug was that ``t3`` invoked with cwd inside a *different* teatree checkout
    imported teatree from THAT checkout's ``src/`` (nearest pyproject wins), then
    crashed on a module the branch had relocated.  This drives the actual
    ``t3_bootstrap.main`` resolution in a subprocess whose cwd is a sibling
    checkout carrying a sentinel ``teatree`` package, and asserts the resolved
    ``teatree.__file__`` is the install/configured tree — never the cwd checkout.
    """

    def _spawn(self, cwd: Path, env: dict[str, str]) -> subprocess.CompletedProcess[str]:
        prog = (
            "import t3_bootstrap, sys;"
            "src = t3_bootstrap._resolve_pinned_source();"
            "sys.path.insert(0, str(src)) if src is not None else None;"
            "import teatree;"
            "print(teatree.__file__)"
        )
        return subprocess.run(
            [sys.executable, "-c", prog],
            cwd=cwd,
            env=env,
            check=True,
            capture_output=True,
            text=True,
        )

    def test_cwd_sibling_checkout_does_not_win_when_env_unset(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        sibling = tmp_path / "ac" / "moves-a-module" / "teatree"
        sibling.mkdir(parents=True)
        _make_teatree_tree(sibling)
        sentinel = sibling / "src" / "teatree" / "__init__.py"
        sentinel.write_text("SENTINEL = 'cwd-checkout'\n")

        env = {k: v for k, v in __import__("os").environ.items() if k != "T3_REPO"}
        env["PYTHONPATH"] = str(_REPO_ROOT / "src")

        result = self._spawn(sibling, env)
        resolved = Path(result.stdout.strip()).resolve()
        assert resolved != sentinel.resolve()
        assert sibling not in resolved.parents

    def test_env_pins_install_tree_over_cwd_checkout(self, tmp_path: Path) -> None:
        sibling = tmp_path / "ac" / "moves-a-module" / "teatree"
        sibling.mkdir(parents=True)
        _make_teatree_tree(sibling)
        sentinel = sibling / "src" / "teatree" / "__init__.py"
        sentinel.write_text("SENTINEL = 'cwd-checkout'\n")

        env = dict(__import__("os").environ)
        env["T3_REPO"] = str(_REPO_ROOT)
        env["PYTHONPATH"] = str(_REPO_ROOT / "src")

        result = self._spawn(sibling, env)
        resolved = Path(result.stdout.strip()).resolve()
        assert resolved == (_REPO_ROOT / "src" / "teatree" / "__init__.py").resolve()
        assert resolved != sentinel.resolve()


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
