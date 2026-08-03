"""``project_env_is_drivable`` — the guard on the ``uv run`` prefix chokepoint.

``uv run`` removes and recreates a ``.venv`` it cannot use, so a repo carrying an
environment built for the other side of a container boundary must not be driven
through :func:`runner_prefix`. Real ``pyvenv.cfg`` files under ``tmp_path``; no mocks.
"""

from pathlib import Path

from teatree.utils.django_db import project_env_is_drivable


def _write_pyvenv_cfg(repo: Path, home: Path | str) -> None:
    venv = repo / ".venv"
    venv.mkdir(parents=True)
    (venv / "pyvenv.cfg").write_text(
        f"home = {home}\nimplementation = CPython\nversion_info = 3.13.12\n",
        encoding="utf-8",
    )


def test_repo_without_a_venv_is_drivable(tmp_path: Path) -> None:
    assert project_env_is_drivable(tmp_path) is True


def test_venv_whose_interpreter_exists_here_is_drivable(tmp_path: Path) -> None:
    interpreter_home = tmp_path / "pythons" / "cpython-3.13" / "bin"
    interpreter_home.mkdir(parents=True)
    repo = tmp_path / "repo"
    repo.mkdir()
    _write_pyvenv_cfg(repo, interpreter_home)

    assert project_env_is_drivable(repo) is True


def test_venv_from_the_other_side_of_the_boundary_is_not_drivable(tmp_path: Path) -> None:
    """The bind-mounted host working tree seen from inside the container."""
    repo = tmp_path / "repo"
    repo.mkdir()
    _write_pyvenv_cfg(repo, "/absent-on-this-side/uv/python/cpython-3.13/bin")

    assert project_env_is_drivable(repo) is False


def test_pyvenv_cfg_without_a_home_key_is_drivable(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    (repo / ".venv").mkdir(parents=True)
    (repo / ".venv" / "pyvenv.cfg").write_text("implementation = CPython\n", encoding="utf-8")

    assert project_env_is_drivable(repo) is True
