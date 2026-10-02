"""Staging an overlay tool's host plan where the wrapper actually reads it.

The container half of a host-hopped tool resolves a plan and writes it into the
bind-mounted data dir; ``deploy/t3`` then runs the staged runner on the host. The
one thing that has to hold is WHERE: a plan written from inside a checkout lands
under an auto-isolated ``teatree-worktrees/<hash>/`` dir the wrapper never looks in
and the worktree reaper deletes, so the handoff silently does nothing.
"""

import shutil
import stat
import subprocess
from pathlib import Path

import pytest

from teatree.core.host_hop import host_run

_RUNNER_BODY = "#!/usr/bin/env bash\nexit 4\n"


@pytest.fixture
def redirected_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    home = tmp_path / "home"
    home.mkdir(exist_ok=True)
    monkeypatch.setattr(host_run.paths, "_TRUE_CANONICAL_DATA_DIR", home / ".local" / "share" / "teatree")
    return home


@pytest.fixture
def runner(tmp_path: Path) -> Path:
    source = tmp_path / "stack-host.sh"
    source.write_text(_RUNNER_BODY, encoding="utf-8")
    return source


def _checkout(tmp_path: Path) -> Path:
    """A real git checkout, so the data-dir resolver takes its worktree-isolating branch."""
    root = tmp_path / "checkout"
    root.mkdir()
    subprocess.run([shutil.which("git") or "git", "init", "-q"], cwd=root, check=True)
    return root


class TestThePlanLandsWhereTheWrapperReadsIt:
    def test_staging_from_inside_a_checkout_still_lands_under_the_canonical_dir(
        self, tmp_path: Path, redirected_home: Path, runner: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.chdir(_checkout(tmp_path))

        staged = host_run.stage(runner, "action=up\n", {})

        assert staged == redirected_home / ".local" / "share" / "teatree" / host_run.PLAN_DIR
        assert "teatree-worktrees" not in str(staged)

    def test_the_plan_and_runner_carry_the_pinned_names(self, redirected_home: Path, runner: Path) -> None:
        staged = host_run.stage(runner, "action=up\n", {})

        assert (staged / host_run.PLAN_NAME).read_text(encoding="utf-8") == "action=up\n"
        assert (staged / host_run.RUNNER_NAME).read_text(encoding="utf-8") == _RUNNER_BODY

    def test_sibling_files_are_written_beside_the_plan(self, redirected_home: Path, runner: Path) -> None:
        staged = host_run.stage(runner, "action=up\n", {"creds.env": "A=1\n"})

        assert (staged / "creds.env").read_text(encoding="utf-8") == "A=1\n"

    def test_a_restaged_plan_replaces_the_previous_one(self, redirected_home: Path, runner: Path) -> None:
        host_run.stage(runner, "action=up\nstale=1\n", {"creds.env": "OLD=1\n"})
        staged = host_run.stage(runner, "action=down\n", {})

        assert (staged / host_run.PLAN_NAME).read_text(encoding="utf-8") == "action=down\n"
        assert not (staged / "creds.env").exists(), "a stale sibling must not survive into the next run"


class TestTheStagedFilesCarryTheModesTheirContentNeeds:
    def test_the_runner_copy_is_executable_and_private(self, redirected_home: Path, runner: Path) -> None:
        staged = host_run.stage(runner, "action=up\n", {})

        assert stat.S_IMODE((staged / host_run.RUNNER_NAME).stat().st_mode) == 0o700

    def test_the_plan_and_its_siblings_are_private_and_not_executable(
        self, redirected_home: Path, runner: Path
    ) -> None:
        staged = host_run.stage(runner, "action=up\n", {"creds.env": "SECRET=1\n"})

        assert stat.S_IMODE((staged / host_run.PLAN_NAME).stat().st_mode) == 0o600
        assert stat.S_IMODE((staged / "creds.env").stat().st_mode) == 0o600


class TestDispatchRunsTheRunnerOnlyWhereItCan:
    def test_in_a_container_it_stages_and_spawns_nothing(
        self, redirected_home: Path, runner: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(host_run, "running_in_container", lambda: True)
        spawned: list[object] = []
        monkeypatch.setattr(host_run, "run_streamed", lambda *a, **k: spawned.append((a, k)) or 0)

        assert host_run.dispatch(runner, "action=up\n", {}) == 0
        assert spawned == []
        assert (host_run.plan_dir() / host_run.PLAN_NAME).exists()

    def test_natively_it_runs_the_staged_runner_and_returns_its_code(
        self, redirected_home: Path, runner: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(host_run, "running_in_container", lambda: False)

        assert host_run.dispatch(runner, "action=up\n", {}) == 4

    def test_natively_the_runner_is_handed_the_plan_directory(
        self, redirected_home: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        witness = tmp_path / "argv"
        source = tmp_path / "echoing-host.sh"
        source.write_text(f'#!/usr/bin/env bash\nprintf "%s" "$1" >{witness}\nexit 0\n', encoding="utf-8")
        monkeypatch.setattr(host_run, "running_in_container", lambda: False)

        assert host_run.dispatch(source, "action=up\n", {}) == 0
        assert witness.read_text(encoding="utf-8") == str(host_run.plan_dir())


def test_the_staged_runner_is_a_copy_not_the_source(redirected_home: Path, runner: Path) -> None:
    staged = host_run.stage(runner, "action=up\n", {})

    assert (staged / host_run.RUNNER_NAME).resolve() != runner.resolve()
    assert not Path(staged / host_run.RUNNER_NAME).samefile(runner)
