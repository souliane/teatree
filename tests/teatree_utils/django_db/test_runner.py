"""``project_env_is_drivable`` — the guard on the ``uv run`` prefix chokepoint.

``uv run`` removes and recreates a ``.venv`` it cannot use, so a repo carrying an
environment built for the other side of a container boundary must not be driven
through :func:`runner_prefix`. Real ``pyvenv.cfg`` files under ``tmp_path``; no mocks.

ABSENCE of the recorded interpreter is no longer the whole test. Once the two
venues share ONE interpreter root at ONE address, the other side's interpreter is
PRESENT here — so the uv platform tag is the only thing left that can tell a
foreign environment from a local one.
"""

import os
import shutil
import subprocess
import sys
import time
import venv
from collections.abc import Iterator
from pathlib import Path

import pytest

from teatree.paths import GENERATION_MARKER
from teatree.utils.django_db import project_env_is_drivable, runner
from teatree.utils.django_db.runner import project_env_import_error, runner_prefix
from tests._uv_stub import executable


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


@pytest.fixture
def uv_project(tmp_path: Path) -> Path:
    repo = tmp_path / "repo"
    (repo / ".venv").mkdir(parents=True)
    (repo / "uv.lock").write_text('version = 1\n\n[[package]]\nname = "django"\nversion = "5.0"\n', encoding="utf-8")
    return repo


class TestUvRunSkipsSyncWhenTheEnvSatisfiesTheLock:
    @pytest.fixture
    def stub_uv(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> tuple[Path, Path]:
        bin_dir = tmp_path / "bin"
        monkeypatch.setenv("PATH", f"{bin_dir}{os.pathsep}{os.environ['PATH']}")
        return bin_dir / "uv", tmp_path / "uv-argv.log"

    def _install(self, stub: tuple[Path, Path], outcome: str) -> Path:
        uv, log = stub
        executable(uv, f'#!/bin/sh\necho "$@" >> "{log}"\n{outcome}\n')
        return log

    def test_a_satisfied_env_runs_without_a_sync(self, uv_project: Path, stub_uv: tuple[Path, Path]) -> None:
        log = self._install(stub_uv, "exit 0")

        assert runner_prefix(uv_project) == ["uv", "--directory", str(uv_project), "run", "--no-sync", "python"]
        assert log.read_text(encoding="utf-8").splitlines() == [
            f"--directory {uv_project} sync --locked --inexact --offline --check"
        ]

    @pytest.mark.parametrize("outcome", ["exit 1", "exit 2", "exec sleep 30"])
    def test_an_unconfirmed_env_keeps_the_syncing_run(
        self, uv_project: Path, stub_uv: tuple[Path, Path], monkeypatch: pytest.MonkeyPatch, outcome: str
    ) -> None:
        self._install(stub_uv, outcome)
        monkeypatch.setattr(runner, "_SYNC_PROBE_TIMEOUT_SECONDS", 0.5)

        started = time.monotonic()
        assert runner_prefix(uv_project) == ["uv", "--directory", str(uv_project), "run", "python"]
        assert time.monotonic() - started < 5

    def test_no_uv_on_path_keeps_the_syncing_run(
        self, uv_project: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        empty = tmp_path / "empty-bin"
        empty.mkdir()
        monkeypatch.setenv("PATH", str(empty))

        assert runner_prefix(uv_project) == ["uv", "--directory", str(uv_project), "run", "python"]

    @pytest.mark.parametrize("missing", [".venv", "uv.lock"])
    def test_a_cold_checkout_is_never_probed(self, uv_project: Path, stub_uv: tuple[Path, Path], missing: str) -> None:
        log = self._install(stub_uv, "exit 0")
        shutil.move(uv_project / missing, uv_project.parent / missing)

        assert runner_prefix(uv_project) == ["uv", "--directory", str(uv_project), "run", "python"]
        assert not log.exists()

    def test_a_pipenv_repo_is_never_probed(self, uv_project: Path, stub_uv: tuple[Path, Path]) -> None:
        log = self._install(stub_uv, "exit 0")
        (uv_project / "Pipfile").write_text("[packages]\ndjango = '*'\n", encoding="utf-8")
        (uv_project / "uv.lock").write_text('version = 1\nrequires-python = ">=3.12"\n', encoding="utf-8")

        assert runner_prefix(uv_project) == [
            "env",
            f"PIPENV_PIPFILE={uv_project / 'Pipfile'}",
            "pipenv",
            "run",
            "python",
        ]
        assert not log.exists()


@pytest.mark.integration
class TestTheSyncProbeAgreesWithRealUv:
    @pytest.fixture
    def synced_project(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
        uv = shutil.which("uv")
        if uv is None:
            pytest.skip("uv not available")
        for key in [k for k in os.environ if k.startswith("UV_") or k == "VIRTUAL_ENV"]:
            monkeypatch.delenv(key)
        monkeypatch.setenv("UV_CACHE_DIR", str(tmp_path / "uv-cache"))
        repo = tmp_path / "project"
        repo.mkdir()
        (repo / "pyproject.toml").write_text(
            '[project]\nname = "probe"\nversion = "0"\nrequires-python = ">=3.10"\ndependencies = []\n',
            encoding="utf-8",
        )
        for step in (["lock"], ["sync", "--python", sys.executable]):
            subprocess.run([uv, "--directory", str(repo), *step, "--offline"], check=True, capture_output=True)
        return repo

    def test_a_synced_project_runs_without_a_sync(self, synced_project: Path) -> None:
        assert "--no-sync" in runner_prefix(synced_project)

    def test_a_stale_lock_keeps_the_syncing_run_and_is_not_relocked(self, synced_project: Path) -> None:
        pyproject = synced_project / "pyproject.toml"
        pyproject.write_text(pyproject.read_text(encoding="utf-8").replace(">=3.10", ">=3.11"), encoding="utf-8")
        lock_before = (synced_project / "uv.lock").read_bytes()

        assert "--no-sync" not in runner_prefix(synced_project)
        assert (synced_project / "uv.lock").read_bytes() == lock_before
