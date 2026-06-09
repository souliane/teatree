"""``teatree.utils.venv_artifacts.find_stale_uv_venv`` (souliane/teatree#2005)."""

from pathlib import Path

import pytest

from teatree.utils.venv_artifacts import find_stale_uv_venv


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
