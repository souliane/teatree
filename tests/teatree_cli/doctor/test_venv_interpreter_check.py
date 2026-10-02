"""``_check_venv_interpreter_is_this_host`` — the `t3 doctor` repointed-venv gate.

The fork-root ``.venv`` is a uv WORKSPACE environment shared by every member and it
sits inside a Docker bind mount, so whichever side ran ``uv`` last writes the
interpreter both sides then read. When that is the other side's, nothing reports a
broken environment: the next ``uv run`` DELETES and rebuilds the venv, and on the
mount that removal can fail half-done (``Directory not empty``), leaving a truncated
install that still imports and reds unrelated gates. This gate states the repoint
instead — loudly, and without repairing, because the repair is the destructive step.

End-to-end against real on-disk venvs under ``tmp_path``, driven through the same
``_collect_repos()`` seam the neighbouring venv gate uses.
"""

import sys
from pathlib import Path
from unittest.mock import patch

from teatree.cli.doctor.checks_environment import _check_venv_interpreter_is_this_host

_HOST_TAG = {"darwin": "macos", "linux": "linux", "win32": "windows"}.get(sys.platform, "macos")


def _repo_with_venv_home(root: Path, name: str, home: Path | str) -> Path:
    repo = root / name
    (repo / ".venv").mkdir(parents=True)
    (repo / ".venv" / "pyvenv.cfg").write_text(
        f"home = {home}\nversion_info = 3.13.12\nuv = 0.11.15\n", encoding="utf-8"
    )
    return repo


class TestVenvInterpreterDoctorCheck:
    def test_repointed_venv_fails_loud_and_names_the_repair(self, tmp_path: Path, capsys) -> None:
        """The observed repoint: a macOS clone whose venv records the container's interpreter."""
        repo = _repo_with_venv_home(
            tmp_path, "workspace-root", "/opt/teatree/uv/python/cpython-3.13-linux-aarch64-gnu/bin"
        )
        with patch("teatree.cli.update._collect_repos", return_value=[("workspace-root", repo)]):
            assert _check_venv_interpreter_is_this_host() is False
        out = capsys.readouterr().out
        assert "FAIL" in out
        assert str(repo) in out
        assert "uv sync" in out

    def test_healthy_host_venv_is_silent(self, tmp_path: Path, capsys) -> None:
        home = tmp_path / "uv" / "python" / f"cpython-3.13-{_HOST_TAG}-aarch64-none" / "bin"
        home.mkdir(parents=True)
        repo = _repo_with_venv_home(tmp_path, "clone", home)
        with patch("teatree.cli.update._collect_repos", return_value=[("clone", repo)]):
            assert _check_venv_interpreter_is_this_host() is True
        assert capsys.readouterr().out == ""

    def test_repo_without_a_venv_is_silent(self, tmp_path: Path, capsys) -> None:
        repo = tmp_path / "no-venv"
        repo.mkdir()
        with patch("teatree.cli.update._collect_repos", return_value=[("no-venv", repo)]):
            assert _check_venv_interpreter_is_this_host() is True
        assert capsys.readouterr().out == ""

    def test_never_repairs_the_repointed_venv(self, tmp_path: Path) -> None:
        """The gate reports; it must not run the rebuild that destroys the environment."""
        repo = _repo_with_venv_home(tmp_path, "clone", "/opt/absent/cpython-3.13-linux-aarch64-gnu/bin")
        with patch("teatree.cli.update._collect_repos", return_value=[("clone", repo)]):
            assert _check_venv_interpreter_is_this_host() is False
        assert (repo / ".venv" / "pyvenv.cfg").is_file()
