import fcntl
import os
from collections.abc import Callable, Iterator
from contextlib import AbstractContextManager, contextmanager, nullcontext
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path

from teatree.harness_skills import (
    HarnessSkillTarget,
    NamedSkillDependency,
    SkillInventoryLike,
    SkillsHarness,
    authorize_harness_skill_removal,
    parse_harness_skill_exclusions,
)
from teatree.provisioning.skills_cli import SkillsCli


class RemovalStage(StrEnum):
    REMOVE = "remove"
    PERSIST = "persist"
    REFRESH = "refresh"


_FAILURE_STATE = {
    RemovalStage.REMOVE: "skill remains installed; no exclusion was persisted",
    RemovalStage.PERSIST: "skill was removed but its exclusion was not persisted, so it may return during setup",
    RemovalStage.REFRESH: "skill was removed and excluded, but the cached inventory is stale",
}


class HarnessSkillRemovalError(RuntimeError):
    def __init__(self, stage: RemovalStage, target: HarnessSkillTarget, cause: Exception) -> None:
        self.stage = stage
        self.target = target
        self.cause = cause
        self.removed = stage is not RemovalStage.REMOVE
        self.exclusion_persisted = stage is RemovalStage.REFRESH
        super().__init__(f"{target.exclusion} removal failed during {stage.value}: {_FAILURE_STATE[stage]}")


@dataclass(frozen=True, slots=True)
class HarnessSkillRemovalResult:
    target: HarnessSkillTarget
    exclusions: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class HarnessSkillRequirements:
    inventory: SkillInventoryLike
    apm_dependencies: tuple[NamedSkillDependency, ...]
    runtime_demands: tuple[str, ...]


PersistExclusions = Callable[[list[str]], None]
RefreshReceipt = Callable[[], object]
OperationLock = Callable[[], AbstractContextManager[None]]


@contextmanager
def file_operation_lock(path: Path) -> Iterator[None]:
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    fd = os.open(path, os.O_CREAT | os.O_RDWR, 0o600)
    os.fchmod(fd, 0o600)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        yield
    finally:
        fcntl.flock(fd, fcntl.LOCK_UN)
        os.close(fd)


@dataclass(slots=True)
class HarnessSkillRemovalService:
    cli: SkillsCli
    persist_exclusions: PersistExclusions
    refresh_receipt: RefreshReceipt
    operation_lock: OperationLock = nullcontext

    def remove_optional(
        self,
        harness: SkillsHarness | str,
        skill: str,
        *,
        load_current_exclusions: Callable[[], object],
        requirements: HarnessSkillRequirements,
    ) -> HarnessSkillRemovalResult:
        target = authorize_harness_skill_removal(
            harness,
            skill,
            inventory=requirements.inventory,
            apm_dependencies=requirements.apm_dependencies,
            runtime_demands=requirements.runtime_demands,
        )
        with self.operation_lock():
            current = load_current_exclusions()
            exclusions = parse_harness_skill_exclusions([*parse_harness_skill_exclusions(current), target.exclusion])
            try:
                self.cli.version()
                self.cli.remove_global(target.skill, target.harness)
            except Exception as error:
                raise HarnessSkillRemovalError(RemovalStage.REMOVE, target, error) from error
            try:
                self.persist_exclusions(exclusions)
            except Exception as error:
                raise HarnessSkillRemovalError(RemovalStage.PERSIST, target, error) from error
            try:
                self.refresh_receipt()
            except Exception as error:
                raise HarnessSkillRemovalError(RemovalStage.REFRESH, target, error) from error
        return HarnessSkillRemovalResult(target=target, exclusions=tuple(exclusions))
