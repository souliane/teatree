"""``teatree.utils.venv_artifacts`` — wrong-toolchain (#2005) and wrong-host venv artifacts."""

from pathlib import Path

import pytest

from teatree.utils.venv_artifacts import find_stale_uv_venv, foreign_venv_interpreter


def _make_venv(repo: Path, *, uv_built: bool, packages: tuple[str, ...] = ()) -> Path:
    """Build a fake in-project ``.venv`` mirroring uv/virtualenv layout.

    *uv_built* writes the ``uv =`` marker line into ``pyvenv.cfg`` that uv (and
    not pipenv/virtualenv) emits. *packages* names installed distributions; an
    empty tuple leaves only the ``_virtualenv.pth`` bootstrap file uv drops into
    a freshly-built, dependency-free venv.
    """
    venv = repo / ".venv"
    site = venv / "lib" / "python3.13" / "site-packages"
    site.mkdir(parents=True)
    cfg = "home = /usr/bin\nversion_info = 3.13.0\n"
    if uv_built:
        cfg += "uv = 0.9.24\n"
    (venv / "pyvenv.cfg").write_text(cfg, encoding="utf-8")
    (site / "_virtualenv.pth").write_text("import _virtualenv\n", encoding="utf-8")
    (site / "_virtualenv.py").write_text("# virtualenv bootstrap\n", encoding="utf-8")
    for pkg in packages:
        (site / f"{pkg}.dist-info").mkdir()
    return venv


class TestFindStaleUvVenv:
    def test_flags_empty_uv_venv_in_pipfile_repo(self, tmp_path: Path) -> None:
        (tmp_path / "Pipfile").write_text("[packages]\n", encoding="utf-8")
        venv = _make_venv(tmp_path, uv_built=True)
        assert find_stale_uv_venv(tmp_path) == venv

    def test_ignores_populated_uv_venv(self, tmp_path: Path) -> None:
        (tmp_path / "Pipfile").write_text("[packages]\n", encoding="utf-8")
        _make_venv(tmp_path, uv_built=True, packages=("django",))
        assert find_stale_uv_venv(tmp_path) is None

    def test_ignores_pipenv_built_venv(self, tmp_path: Path) -> None:
        (tmp_path / "Pipfile").write_text("[packages]\n", encoding="utf-8")
        _make_venv(tmp_path, uv_built=False)
        assert find_stale_uv_venv(tmp_path) is None

    def test_ignores_uv_managed_repo_without_pipfile(self, tmp_path: Path) -> None:
        _make_venv(tmp_path, uv_built=True)
        assert find_stale_uv_venv(tmp_path) is None

    def test_returns_none_without_venv(self, tmp_path: Path) -> None:
        (tmp_path / "Pipfile").write_text("[packages]\n", encoding="utf-8")
        assert find_stale_uv_venv(tmp_path) is None

    def test_non_dir_site_packages_match_is_skipped(self, tmp_path: Path) -> None:
        """A stray file named ``site-packages`` is not iterated (kept empty)."""
        (tmp_path / "Pipfile").write_text("[packages]\n", encoding="utf-8")
        venv = _make_venv(tmp_path, uv_built=True)
        (venv / "site-packages").write_text("not a directory\n", encoding="utf-8")
        assert find_stale_uv_venv(tmp_path) == venv

    def test_unreadable_pyvenv_cfg_is_ignored(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        (tmp_path / "Pipfile").write_text("[packages]\n", encoding="utf-8")
        venv = _make_venv(tmp_path, uv_built=True)

        def _boom(*_args: object, **_kwargs: object) -> str:
            raise OSError

        monkeypatch.setattr(Path, "read_text", _boom)
        assert find_stale_uv_venv(tmp_path) is None
        assert venv.exists()


def _venv_with_home(root: Path, home: Path | str) -> Path:
    """A ``.venv`` whose ``pyvenv.cfg`` records *home* as its interpreter directory."""
    venv = root / ".venv"
    venv.mkdir(parents=True, exist_ok=True)
    (venv / "pyvenv.cfg").write_text(f"home = {home}\nversion_info = 3.13.12\nuv = 0.11.15\n", encoding="utf-8")
    return venv


class TestForeignVenvInterpreter:
    """A ``.venv`` in a bind mount can record the interpreter of the other side."""

    def test_absent_home_is_foreign(self, tmp_path: Path) -> None:
        """The container-built venv seen from the host: the recorded dir is not here."""
        venv = _venv_with_home(tmp_path, "/opt/teatree/uv/python/cpython-3.13-linux-aarch64-gnu/bin")
        reason = foreign_venv_interpreter(venv, platform="darwin")
        assert reason is not None
        assert "does not exist on this host" in reason

    def test_present_home_of_another_platform_is_foreign(self, tmp_path: Path) -> None:
        """A shared mount makes the dir resolvable, so the uv platform tag has to decide."""
        home = tmp_path / "uv" / "python" / "cpython-3.13-linux-aarch64-gnu" / "bin"
        home.mkdir(parents=True)
        reason = foreign_venv_interpreter(_venv_with_home(tmp_path, home), platform="darwin")
        assert reason is not None
        assert "is a linux interpreter; this host is darwin" in reason

    def test_matching_platform_is_healthy(self, tmp_path: Path) -> None:
        home = tmp_path / "uv" / "python" / "cpython-3.13-macos-aarch64-none" / "bin"
        home.mkdir(parents=True)
        assert foreign_venv_interpreter(_venv_with_home(tmp_path, home), platform="darwin") is None

    def test_untagged_system_interpreter_is_not_judged(self, tmp_path: Path) -> None:
        """A system interpreter carries no uv platform tag — absence of proof, not a repoint."""
        home = tmp_path / "usr" / "bin"
        home.mkdir(parents=True)
        assert foreign_venv_interpreter(_venv_with_home(tmp_path, home), platform="darwin") is None

    @pytest.mark.parametrize("body", ["", "version_info = 3.13.12\n"])
    def test_missing_pyvenv_cfg_or_home_is_not_judged(self, tmp_path: Path, body: str) -> None:
        venv = tmp_path / ".venv"
        venv.mkdir()
        if body:
            (venv / "pyvenv.cfg").write_text(body, encoding="utf-8")
        assert foreign_venv_interpreter(venv, platform="darwin") is None
