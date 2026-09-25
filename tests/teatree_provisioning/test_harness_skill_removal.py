import subprocess
import threading
from collections import deque
from collections.abc import Sequence
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from pathlib import Path

import pytest

from teatree.harness_skills import HarnessSkillRequiredError, SkillsHarness
from teatree.provisioning.harness_skill_removal import (
    HarnessSkillRemovalError,
    HarnessSkillRemovalService,
    HarnessSkillRequirements,
    RemovalStage,
)
from teatree.provisioning.skills_cli import SkillsCli
from teatree.skill_support.inventory import SkillInventory


@dataclass(frozen=True)
class NamedDependency:
    name: str


class RecordedRunner:
    def __init__(self, *responses: subprocess.CompletedProcess[str]) -> None:
        self.responses = deque(responses)
        self.calls: list[tuple[str, ...]] = []

    def __call__(self, command: Sequence[str]) -> subprocess.CompletedProcess[str]:
        self.calls.append(tuple(command))
        return self.responses.popleft()


def _completed(
    stdout: str = "",
    *,
    returncode: int = 0,
    stderr: str = "",
) -> subprocess.CompletedProcess[str]:
    return subprocess.CompletedProcess(("skills",), returncode, stdout, stderr)


def _inventory() -> SkillInventory:
    return SkillInventory((), (), (), (), ())


def _requirements(
    *,
    apm_dependencies: tuple[NamedDependency, ...] = (),
    runtime_demands: tuple[str, ...] = (),
) -> HarnessSkillRequirements:
    return HarnessSkillRequirements(_inventory(), apm_dependencies, runtime_demands)


def test_removal_orders_cli_persistence_and_receipt_refresh() -> None:
    events: list[object] = []

    def runner(command: Sequence[str]) -> subprocess.CompletedProcess[str]:
        events.append(tuple(command))
        return _completed("1.7.0\n" if command[-1] == "--version" else "")

    def persist(exclusions: list[str]) -> None:
        events.append(("persist", exclusions))

    def refresh() -> None:
        events.append("refresh")

    result = HarnessSkillRemovalService(
        cli=SkillsCli(runner=runner, home=Path("/nonexistent")),
        persist_exclusions=persist,
        refresh_receipt=refresh,
    ).remove_optional(
        SkillsHarness.CODEX,
        "Review",
        load_current_exclusions=lambda: ["claude-code:test", "codex:review"],
        requirements=_requirements(),
    )

    assert result.target.skill == "review"
    assert result.exclusions == ("claude-code:test", "codex:review")
    assert events == [
        ("skills", "--version"),
        ("skills", "remove", "review", "-g", "-a", "codex", "-y"),
        ("persist", ["claude-code:test", "codex:review"]),
        "refresh",
    ]


def test_required_skill_fails_before_cli_store_or_refresh() -> None:
    runner = RecordedRunner()
    persisted: list[list[str]] = []
    refreshed: list[bool] = []
    service = HarnessSkillRemovalService(
        cli=SkillsCli(runner=runner, home=Path("/nonexistent")),
        persist_exclusions=persisted.append,
        refresh_receipt=lambda: refreshed.append(True),
    )

    with pytest.raises(HarnessSkillRequiredError):
        service.remove_optional(
            SkillsHarness.CLAUDE_CODE,
            "runtime-required",
            load_current_exclusions=list,
            requirements=_requirements(runtime_demands=("runtime-required",)),
        )

    assert runner.calls == []
    assert persisted == []
    assert refreshed == []


def test_version_mismatch_fails_before_remove_store_or_refresh() -> None:
    runner = RecordedRunner(_completed("1.8.0\n"))
    persisted: list[list[str]] = []
    refreshed: list[bool] = []
    service = HarnessSkillRemovalService(
        cli=SkillsCli(runner=runner, home=Path("/nonexistent")),
        persist_exclusions=persisted.append,
        refresh_receipt=lambda: refreshed.append(True),
    )

    with pytest.raises(HarnessSkillRemovalError, match="remains installed") as error:
        service.remove_optional(
            SkillsHarness.CODEX,
            "review",
            load_current_exclusions=list,
            requirements=_requirements(),
        )

    assert error.value.stage is RemovalStage.REMOVE
    assert runner.calls == [("skills", "--version")]
    assert persisted == []
    assert refreshed == []


def test_cli_failure_reports_not_removed_and_skips_later_stages() -> None:
    runner = RecordedRunner(_completed("1.7.0\n"), _completed(returncode=7, stderr="remove failed"))
    persisted: list[list[str]] = []
    refreshed: list[bool] = []
    service = HarnessSkillRemovalService(
        cli=SkillsCli(runner=runner, home=Path("/nonexistent")),
        persist_exclusions=persisted.append,
        refresh_receipt=lambda: refreshed.append(True),
    )

    with pytest.raises(HarnessSkillRemovalError, match="remains installed") as error:
        service.remove_optional(
            SkillsHarness.CODEX,
            "review",
            load_current_exclusions=list,
            requirements=_requirements(),
        )

    assert error.value.stage is RemovalStage.REMOVE
    assert error.value.removed is False
    assert error.value.exclusion_persisted is False
    assert persisted == []
    assert refreshed == []


def test_persistence_failure_reports_removed_without_exclusion_or_refresh() -> None:
    runner = RecordedRunner(_completed("1.7.0\n"), _completed())
    refreshed: list[bool] = []

    def fail_persist(_exclusions: list[str]) -> None:
        message = "database unavailable"
        raise RuntimeError(message)

    service = HarnessSkillRemovalService(
        cli=SkillsCli(runner=runner, home=Path("/nonexistent")),
        persist_exclusions=fail_persist,
        refresh_receipt=lambda: refreshed.append(True),
    )

    with pytest.raises(HarnessSkillRemovalError, match="may return") as error:
        service.remove_optional(
            SkillsHarness.CLAUDE_CODE,
            "review",
            load_current_exclusions=list,
            requirements=_requirements(),
        )

    assert error.value.stage is RemovalStage.PERSIST
    assert error.value.removed is True
    assert error.value.exclusion_persisted is False
    assert refreshed == []


def test_refresh_failure_reports_removed_excluded_and_stale_receipt() -> None:
    runner = RecordedRunner(_completed("1.7.0\n"), _completed())
    persisted: list[list[str]] = []

    def fail_refresh() -> None:
        message = "refresh unavailable"
        raise RuntimeError(message)

    service = HarnessSkillRemovalService(
        cli=SkillsCli(runner=runner, home=Path("/nonexistent")),
        persist_exclusions=persisted.append,
        refresh_receipt=fail_refresh,
    )

    with pytest.raises(HarnessSkillRemovalError, match="cached inventory is stale") as error:
        service.remove_optional(
            SkillsHarness.CODEX,
            "review",
            load_current_exclusions=list,
            requirements=_requirements(),
        )

    assert error.value.stage is RemovalStage.REFRESH
    assert error.value.removed is True
    assert error.value.exclusion_persisted is True
    assert persisted == [["codex:review"]]


def test_concurrent_removals_merge_exclusions_under_one_operation_lock() -> None:
    exclusions: list[str] = []
    lock = threading.Lock()

    def remove(skill: str) -> None:
        runner = RecordedRunner(_completed("1.7.0\n"), _completed())
        HarnessSkillRemovalService(
            cli=SkillsCli(runner=runner, home=Path("/nonexistent")),
            persist_exclusions=lambda value: exclusions.__setitem__(slice(None), value),
            refresh_receipt=lambda: None,
            operation_lock=lambda: lock,
        ).remove_optional(
            SkillsHarness.CODEX,
            skill,
            load_current_exclusions=lambda: list(exclusions),
            requirements=_requirements(),
        )

    with ThreadPoolExecutor(max_workers=2) as executor:
        list(executor.map(remove, ("alpha", "beta")))

    assert exclusions == ["codex:alpha", "codex:beta"]
