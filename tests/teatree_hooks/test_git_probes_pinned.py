# test-path: cross-cutting — drives hooks/scripts git probes against a planted checkout; no src/teatree/ mirror.
"""A hook's git probe must not run a ``core.fsmonitor`` / ``core.hooksPath`` command planted in the checkout.

The hooks are stdlib-only processes outside the ``teatree.utils.run`` chokepoint, so each carries the pin itself.
"""

import time
from collections.abc import Callable
from pathlib import Path

import pytest

from hooks.scripts import git_pin_args, hook_budget
from hooks.scripts.coverage_gate import _git_probe as coverage_gate_probe
from hooks.scripts.foreign_branch_push_git import git_probe as foreign_branch_probe
from hooks.scripts.session_end_work_check import _git as session_end_probe
from hooks.scripts.stop_snapshot_slot import git_state_for_repo
from teatree.utils.git_pins import GitPins
from tests._git_repo import make_git_repo, run_git
from tests._planted_git_config import PlantedConfig, plant_exec_config

# True when git answered, so a budget-starved probe cannot pass the marker assertion vacuously.
Probe = Callable[..., bool]

PROBES: dict[str, Probe] = {
    "session_end_work_check": lambda repo, *args: session_end_probe(repo, *args) is not None,
    "coverage_gate": lambda repo, *args: coverage_gate_probe(repo, list(args)) is not None,
    "foreign_branch_push_git": lambda repo, *args: foreign_branch_probe(str(repo), *args).ok,
}


@pytest.fixture(autouse=True)
def _fresh_hook_budget(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(hook_budget, "_STARTED_AT", time.monotonic())


@pytest.fixture
def planted(tmp_path: Path) -> tuple[Path, PlantedConfig]:
    repo = make_git_repo(tmp_path / "repo")
    (repo / "tracked.txt").write_text("base\n", encoding="utf-8")
    run_git(repo, "add", "tracked.txt")
    run_git(repo, "commit", "-q", "-m", "base")
    (repo / "tracked.txt").write_text("changed\n", encoding="utf-8")
    return repo, plant_exec_config(repo, tmp_path)


def test_the_hook_pin_is_the_control_plane_pin_for_a_verb_that_fires_no_gate_hook() -> None:
    assert GitPins.argv(["git", "status"]) == ["git", *git_pin_args.GIT_PINS, "status"]


@pytest.mark.parametrize("probe", PROBES)
def test_reading_the_index_runs_no_planted_fsmonitor(planted: tuple[Path, PlantedConfig], probe: str) -> None:
    repo, config = planted

    assert PROBES[probe](repo, "status", "--porcelain")
    assert config.fired == []


@pytest.mark.parametrize("probe", PROBES)
def test_a_checkout_runs_no_planted_hook(planted: tuple[Path, PlantedConfig], probe: str) -> None:
    repo, config = planted

    assert PROBES[probe](repo, "checkout", "-q", "-b", "side")
    assert config.fired == []


def test_stop_snapshot_reads_the_git_state_without_running_a_planted_fsmonitor(
    planted: tuple[Path, PlantedConfig],
) -> None:
    repo, config = planted

    state = git_state_for_repo(repo)

    assert state is not None
    assert state["uncommitted"] == "1"
    assert config.fired == []


def test_a_bare_git_does_run_the_planted_config(planted: tuple[Path, PlantedConfig]) -> None:
    """Control: the plant is live for the exact argv shape the probes use."""
    repo, config = planted

    run_git(repo, "--no-optional-locks", "status", "--porcelain")

    assert "fsmonitor" in config.fired
