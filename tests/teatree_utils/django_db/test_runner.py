"""``project_env_is_drivable`` — the guard on the ``uv run`` prefix chokepoint.

``uv run`` removes and recreates a ``.venv`` it cannot use, so a repo carrying an
environment built for the other side of a container boundary must not be driven
through :func:`runner_prefix`. Real ``pyvenv.cfg`` files under ``tmp_path``; no mocks.

ABSENCE of the recorded interpreter is no longer the whole test. Once the two
venues share ONE interpreter root at ONE address, the other side's interpreter is
PRESENT here — so the uv platform tag is the only thing left that can tell a
foreign environment from a local one.
"""

import subprocess
import sys
import venv
from collections.abc import Iterator
from pathlib import Path

import pytest

from teatree.paths import GENERATION_MARKER
from teatree.utils.django_db import project_env_is_drivable
from teatree.utils.django_db.runner import project_env_import_error, runner_prefix


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


def test_venv_on_a_shared_root_naming_another_platform_is_not_drivable(tmp_path: Path) -> None:
    """The case the shared interpreter root CREATES, where existence proves nothing."""
    foreign_os = "linux" if sys.platform != "linux" else "macos"
    interpreter_home = tmp_path / "uv" / "python" / f"cpython-3.13.12-{foreign_os}-aarch64-none" / "bin"
    interpreter_home.mkdir(parents=True)
    repo = tmp_path / "repo"
    repo.mkdir()
    _write_pyvenv_cfg(repo, interpreter_home)

    # The control: without this the test would pass for the OLD reason (absence),
    # proving nothing about the platform tag.
    assert interpreter_home.is_dir()
    assert project_env_is_drivable(repo) is False


class TestProjectEnvImportError:
    def test_a_venv_without_django_names_the_import_failure(self, tmp_path: Path) -> None:
        venv.create(tmp_path / ".venv", with_pip=False, symlinks=True)
        assert project_env_import_error(tmp_path) == "ModuleNotFoundError: No module named 'django'"

    def test_a_venv_that_imports_django_has_nothing_to_report(self, tmp_path: Path) -> None:
        interpreter = tmp_path / ".venv" / "bin" / "python"
        interpreter.parent.mkdir(parents=True)
        interpreter.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
        interpreter.chmod(0o755)
        assert project_env_import_error(tmp_path) is None

    def test_a_repo_without_a_venv_has_nothing_to_report(self, tmp_path: Path) -> None:
        assert project_env_import_error(tmp_path) is None


@pytest.fixture
def sealed_generation(tmp_path: Path) -> Iterator[Path]:
    root = tmp_path / "baked"
    project = root / "overlay"
    project.mkdir(parents=True)
    (root / "pyproject.toml").write_text('[project]\nname = "fork"\nversion = "0"\n', encoding="utf-8")
    (project / "pyproject.toml").write_text('[project]\nname = "overlay"\nversion = "0"\n', encoding="utf-8")
    (project / "manage.py").write_text("import sys\nprint(sys.executable)\n", encoding="utf-8")
    (root / GENERATION_MARKER).write_text("c" * 40 + "\n", encoding="utf-8")
    sealed = [root, project, *root.rglob("*")]
    for path in sealed:
        path.chmod(path.stat().st_mode & ~0o222)
    yield project
    for path in sealed:
        path.chmod(path.stat().st_mode | 0o200)


class TestABakedGenerationRunsInItsOwnInterpreter:
    def test_a_project_inside_a_sealed_generation_runs_manage_py_without_a_project_env(
        self, sealed_generation: Path
    ) -> None:
        result = subprocess.run(
            [*runner_prefix(sealed_generation), "manage.py"],
            cwd=sealed_generation,
            capture_output=True,
            text=True,
            check=False,
        )

        assert result.returncode == 0, result.stderr
        assert result.stdout.strip() == sys.executable
        assert not (sealed_generation / ".venv").exists()

    def test_a_checkout_keeps_the_uv_project_env(self, tmp_path: Path) -> None:
        assert runner_prefix(tmp_path) == ["uv", "--directory", str(tmp_path), "run", "python"]
