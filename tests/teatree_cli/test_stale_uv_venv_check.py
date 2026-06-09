"""``_check_stale_uv_venv`` — the `t3 doctor` empty-uv-venv gate (#2005).

End-to-end against a real on-disk repo under ``tmp_path``: the check walks
``_collect_repos()``, detects an empty uv-built ``.venv`` in a Pipfile-managed
clone, removes it, and WARNs. A clean repo (no Pipfile, populated venv, or
pipenv-built venv) is silent.
"""

from pathlib import Path
from unittest.mock import patch

from teatree.cli._doctor_checks import _check_stale_uv_venv


def _pipfile_repo(root: Path, name: str) -> Path:
    repo = root / name
    (repo).mkdir()
    (repo / "Pipfile").write_text("[packages]\n", encoding="utf-8")
    return repo


def _uv_venv(repo: Path, *, packages: tuple[str, ...] = ()) -> Path:
    venv = repo / ".venv"
    site = venv / "lib" / "python3.13" / "site-packages"
    site.mkdir(parents=True)
    (venv / "pyvenv.cfg").write_text("home = /usr/bin\nuv = 0.9.24\n", encoding="utf-8")
    (site / "_virtualenv.pth").write_text("import _virtualenv\n", encoding="utf-8")
    for pkg in packages:
        (site / f"{pkg}.dist-info").mkdir()
    return venv


class TestStaleUvVenvDoctorCheck:
    def test_removes_empty_uv_venv_and_warns(self, tmp_path, capsys):
        repo = _pipfile_repo(tmp_path, "clone")
        venv = _uv_venv(repo)
        with patch("teatree.cli.update._collect_repos", return_value=[("clone", repo)]):
            assert _check_stale_uv_venv() is False
        out = capsys.readouterr().out
        assert "WARN" in out
        assert str(repo) in out
        assert not venv.exists()

    def test_populated_uv_venv_is_silent_and_kept(self, tmp_path, capsys):
        repo = _pipfile_repo(tmp_path, "clone")
        venv = _uv_venv(repo, packages=("django",))
        with patch("teatree.cli.update._collect_repos", return_value=[("clone", repo)]):
            assert _check_stale_uv_venv() is True
        assert capsys.readouterr().out == ""
        assert venv.exists()

    def test_uv_managed_repo_without_pipfile_is_silent(self, tmp_path, capsys):
        repo = tmp_path / "uv-clone"
        repo.mkdir()
        venv = _uv_venv(repo)
        with patch("teatree.cli.update._collect_repos", return_value=[("uv-clone", repo)]):
            assert _check_stale_uv_venv() is True
        assert capsys.readouterr().out == ""
        assert venv.exists()

    def test_removal_failure_degrades_to_warn(self, tmp_path, capsys):
        repo = _pipfile_repo(tmp_path, "clone")
        venv = _uv_venv(repo)
        with (
            patch("teatree.cli.update._collect_repos", return_value=[("clone", repo)]),
            patch("shutil.rmtree", side_effect=OSError("permission denied")),
        ):
            assert _check_stale_uv_venv() is False
        out = capsys.readouterr().out
        assert "WARN" in out
        assert "manually" in out
        assert venv.exists()
