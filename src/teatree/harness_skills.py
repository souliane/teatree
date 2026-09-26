import re
from collections.abc import Iterable
from dataclasses import dataclass
from enum import StrEnum
from typing import Protocol


class SkillsHarness(StrEnum):
    CLAUDE_CODE = "claude-code"
    CODEX = "codex"


class HarnessSkillPolicyError(ValueError):
    def __init__(self, detail: str) -> None:
        self.detail = detail
        super().__init__(f"invalid harness skill policy value: {detail}")


@dataclass(frozen=True, order=True, slots=True)
class HarnessSkillTarget:
    harness: SkillsHarness
    skill: str

    @property
    def exclusion(self) -> str:
        return f"{self.harness.value}:{self.skill}"


class HarnessSkillRequiredError(HarnessSkillPolicyError):
    def __init__(self, target: HarnessSkillTarget, required_by: tuple[str, ...]) -> None:
        self.target = target
        self.required_by = required_by
        sources = ", ".join(required_by)
        super().__init__(f"{target.exclusion} is required by {sources} and cannot be removed")


class _Diagnostic(Protocol):
    @property
    def code(self) -> object: ...

    @property
    def reference(self) -> str: ...


class SkillInventoryLike(Protocol):
    @property
    def missing_references(self) -> tuple[_Diagnostic, ...]: ...


class NamedSkillDependency(Protocol):
    @property
    def name(self) -> str: ...


_REQUIRED_DIAGNOSTICS = frozenset(
    {
        "missing_dependency",
        "missing_agent_skill",
        "missing_agent_companion",
    }
)
_SKILL_NAME = re.compile(r"[a-z0-9][a-z0-9._-]*")


def _policy_error(detail: str) -> HarnessSkillPolicyError:
    return HarnessSkillPolicyError(detail)


def normalize_harness_skill_name(raw: str) -> str:
    name = raw.rsplit(":", 1)[-1].strip().casefold()
    if name in {"", ".", ".."} or _SKILL_NAME.fullmatch(name) is None:
        detail = f"skill name {raw!r} must be concrete"
        raise _policy_error(detail)
    return name


def harness_skill_target(harness: SkillsHarness | str, skill: str) -> HarnessSkillTarget:
    try:
        normalized_harness = SkillsHarness(str(harness).strip().casefold())
    except ValueError as error:
        detail = f"unsupported harness {harness!r}"
        raise _policy_error(detail) from error
    return HarnessSkillTarget(normalized_harness, normalize_harness_skill_name(skill))


def _parse_exclusion(entry: object) -> HarnessSkillTarget:
    if not isinstance(entry, str):
        detail = f"exclusion entry {entry!r} must be a string"
        raise _policy_error(detail)
    harness, separator, skill = entry.strip().partition(":")
    if not separator:
        detail = f"exclusion entry {entry!r} must use <harness>:<skill>"
        raise _policy_error(detail)
    return harness_skill_target(harness, skill)


def parse_harness_skill_exclusions(raw: object) -> list[str]:
    if not isinstance(raw, list):
        detail = "harness skill exclusions must be a JSON list"
        raise _policy_error(detail)
    return sorted({target.exclusion for target in map(_parse_exclusion, raw)})


def _diagnostic_code(diagnostic: _Diagnostic) -> str:
    return str(getattr(diagnostic.code, "value", diagnostic.code))


def _required_sources(
    inventory: SkillInventoryLike,
    apm_dependencies: Iterable[NamedSkillDependency],
    runtime_demands: Iterable[str],
) -> dict[str, set[str]]:
    sources: dict[str, set[str]] = {}
    for diagnostic in inventory.missing_references:
        code = _diagnostic_code(diagnostic)
        if code in _REQUIRED_DIAGNOSTICS:
            sources.setdefault(normalize_harness_skill_name(diagnostic.reference), set()).add(code)
    for dependency in apm_dependencies:
        sources.setdefault(normalize_harness_skill_name(dependency.name), set()).add("apm.yml")
    for demand in runtime_demands:
        sources.setdefault(normalize_harness_skill_name(demand), set()).add("runtime")
    return sources


def required_harness_skill_names(
    inventory: SkillInventoryLike,
    apm_dependencies: Iterable[NamedSkillDependency],
    runtime_demands: Iterable[str],
) -> tuple[str, ...]:
    return tuple(sorted(_required_sources(inventory, apm_dependencies, runtime_demands)))


def authorize_harness_skill_removal(
    harness: SkillsHarness | str,
    skill: str,
    *,
    inventory: SkillInventoryLike,
    apm_dependencies: Iterable[NamedSkillDependency],
    runtime_demands: Iterable[str],
) -> HarnessSkillTarget:
    target = harness_skill_target(harness, skill)
    sources = _required_sources(inventory, apm_dependencies, runtime_demands).get(target.skill)
    if sources:
        raise HarnessSkillRequiredError(target, tuple(sorted(sources)))
    return target


def remove_harness_skill_exclusion(
    exclusions: object,
    harness: SkillsHarness | str,
    skill: str,
) -> list[str]:
    target = harness_skill_target(harness, skill)
    return [entry for entry in parse_harness_skill_exclusions(exclusions) if entry != target.exclusion]
