from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from typing import Self

import yaml

from teatree.skill_support.agent_declarations import AgentSkillDeclarations, agent_skill_declarations


@dataclass(frozen=True, order=True, slots=True)
class SkillIdentity:
    namespace: str
    manifest_name: str

    @property
    def qualified_name(self) -> str:
        return f"{self.namespace}:{self.manifest_name}"


@dataclass(frozen=True, slots=True)
class SkillDeclaration:
    identity: SkillIdentity
    source_path: Path
    declared_requires: tuple[str, ...]
    direct_requires: tuple[SkillIdentity, ...]
    transitive_requires: tuple[SkillIdentity, ...]


class SkillInventoryDiagnosticCode(StrEnum):
    MISSING_DEPENDENCY = "missing_dependency"
    MISSING_AGENT_SKILL = "missing_agent_skill"
    MISSING_AGENT_COMPANION = "missing_agent_companion"
    DUPLICATE_CANONICAL_IDENTITY = "duplicate_canonical_identity"
    MALFORMED_FRONTMATTER = "malformed_frontmatter"
    MISSING_MANIFEST_NAME = "missing_manifest_name"


@dataclass(frozen=True, slots=True)
class SkillInventoryDiagnostic:
    code: SkillInventoryDiagnosticCode
    subject: str
    reference: str = ""
    source_paths: tuple[Path, ...] = ()


class SkillInventoryLoadError(ValueError):
    def __init__(self, diagnostics: tuple[SkillInventoryDiagnostic, ...]) -> None:
        self.diagnostics = diagnostics
        details = "; ".join(f"{diagnostic.code.value} {diagnostic.subject}" for diagnostic in diagnostics)
        super().__init__(f"skill inventory load failed: {details}")


@dataclass(frozen=True, slots=True)
class EmbeddedSkillClosure:
    context: str
    declared: tuple[SkillIdentity, ...]
    companion_skills: tuple[SkillIdentity, ...]
    embedded: tuple[SkillIdentity, ...]
    missing_references: tuple[SkillInventoryDiagnostic, ...]


@dataclass(frozen=True, slots=True)
class _SkillResolutionIndex:
    by_name: Mapping[str, SkillIdentity]
    by_qualified_name: Mapping[str, SkillIdentity]
    transitive_by_identity: Mapping[SkillIdentity, tuple[SkillIdentity, ...]]


@dataclass(frozen=True, slots=True)
class DependencyCycle:
    identities: tuple[SkillIdentity, ...]


@dataclass(frozen=True, slots=True)
class HarnessSkillInstallation:
    harness: str
    identity: SkillIdentity
    source_path: Path


@dataclass(frozen=True, slots=True)
class SkillNameCollision:
    normalized_manifest_name: str
    teatree_skill: SkillDeclaration
    harness_skill: HarnessSkillInstallation


@dataclass(frozen=True, slots=True)
class SkillInventory:
    declarations: tuple[SkillDeclaration, ...]
    missing_references: tuple[SkillInventoryDiagnostic, ...]
    cycles: tuple[DependencyCycle, ...]
    agent_closures: tuple[EmbeddedSkillClosure, ...]
    phase_closures: tuple[EmbeddedSkillClosure, ...]

    @classmethod
    def load(
        cls,
        *,
        namespace: str,
        skills_dir: Path,
        agents_dir: Path,
        phase_agents: Mapping[str, str],
    ) -> Self:
        parsed = tuple(_parse_skill(skill_md, namespace) for skill_md in sorted(skills_dir.glob("*/SKILL.md")))
        paths_by_identity: dict[SkillIdentity, list[Path]] = {}
        for identity, source_path, _ in parsed:
            paths_by_identity.setdefault(identity, []).append(source_path)
        duplicate_diagnostics = tuple(
            SkillInventoryDiagnostic(
                code=SkillInventoryDiagnosticCode.DUPLICATE_CANONICAL_IDENTITY,
                subject=identity.qualified_name,
                source_paths=tuple(paths),
            )
            for identity, paths in paths_by_identity.items()
            if len(paths) > 1
        )
        if duplicate_diagnostics:
            raise SkillInventoryLoadError(duplicate_diagnostics)
        by_name = {identity.manifest_name: identity for identity, _, _ in parsed}
        by_qualified_name = {identity.qualified_name: identity for identity, _, _ in parsed}
        direct_by_identity = {
            identity: tuple(
                resolved
                for reference in declared_requires
                if (resolved := _resolve_reference(reference, by_name, by_qualified_name)) is not None
            )
            for identity, _, declared_requires in parsed
        }
        transitive_by_identity = {
            identity: _transitive_requires(identity, direct_by_identity) for identity, _, _ in parsed
        }
        resolution_index = _SkillResolutionIndex(
            by_name=by_name,
            by_qualified_name=by_qualified_name,
            transitive_by_identity=transitive_by_identity,
        )
        declarations = tuple(
            SkillDeclaration(
                identity=identity,
                source_path=source_path,
                declared_requires=declared_requires,
                direct_requires=direct_by_identity[identity],
                transitive_requires=transitive_by_identity[identity],
            )
            for identity, source_path, declared_requires in parsed
        )
        dependency_diagnostics = tuple(
            SkillInventoryDiagnostic(
                code=SkillInventoryDiagnosticCode.MISSING_DEPENDENCY,
                subject=identity.qualified_name,
                reference=reference,
                source_paths=(source_path,),
            )
            for identity, source_path, declared_requires in parsed
            for reference in declared_requires
            if _resolve_reference(reference, by_name, by_qualified_name) is None
        )
        cycles = _dependency_cycles(direct_by_identity)
        agent_closures = tuple(
            _embedded_closure(
                agent_path.stem,
                agent_skill_declarations(agent_path),
                agent_path,
                resolution_index,
            )
            for agent_path in sorted(agents_dir.glob("*.md"))
        )
        agents_by_name = {closure.context: closure for closure in agent_closures}
        phase_closures = tuple(
            EmbeddedSkillClosure(
                context=phase,
                declared=agents_by_name[agent].declared,
                companion_skills=agents_by_name[agent].companion_skills,
                embedded=agents_by_name[agent].embedded,
                missing_references=agents_by_name[agent].missing_references,
            )
            for phase, agent in sorted(phase_agents.items())
            if agent in agents_by_name
        )
        return cls(
            declarations=declarations,
            missing_references=dependency_diagnostics
            + tuple(diagnostic for closure in agent_closures for diagnostic in closure.missing_references),
            cycles=cycles,
            agent_closures=agent_closures,
            phase_closures=phase_closures,
        )

    def same_name_hazards(
        self,
        harness_skills: Iterable[HarnessSkillInstallation],
    ) -> tuple[SkillNameCollision, ...]:
        return tuple(
            SkillNameCollision(
                normalized_manifest_name=_normalize_manifest_name(harness_skill.identity.manifest_name),
                teatree_skill=teatree_skill,
                harness_skill=harness_skill,
            )
            for harness_skill in harness_skills
            for teatree_skill in self.declarations
            if _normalize_manifest_name(harness_skill.identity.manifest_name)
            == _normalize_manifest_name(teatree_skill.identity.manifest_name)
        )


def _parse_skill(skill_md: Path, namespace: str) -> tuple[SkillIdentity, Path, tuple[str, ...]]:
    try:
        text = skill_md.read_text(encoding="utf-8")
    except OSError as error:
        raise _manifest_load_error(SkillInventoryDiagnosticCode.MALFORMED_FRONTMATTER, skill_md) from error
    lines = text.splitlines()
    if not lines or lines[0] != "---":
        raise _manifest_load_error(SkillInventoryDiagnosticCode.MALFORMED_FRONTMATTER, skill_md)
    try:
        closing_fence = lines.index("---", 1)
    except ValueError as error:
        raise _manifest_load_error(SkillInventoryDiagnosticCode.MALFORMED_FRONTMATTER, skill_md) from error
    try:
        manifest = yaml.safe_load("\n".join(lines[1:closing_fence]))
    except yaml.YAMLError as error:
        raise _manifest_load_error(SkillInventoryDiagnosticCode.MALFORMED_FRONTMATTER, skill_md) from error
    if not isinstance(manifest, Mapping):
        raise _manifest_load_error(SkillInventoryDiagnosticCode.MALFORMED_FRONTMATTER, skill_md)
    manifest_name = manifest.get("name")
    if not isinstance(manifest_name, str) or not manifest_name.strip():
        raise _manifest_load_error(SkillInventoryDiagnosticCode.MISSING_MANIFEST_NAME, skill_md)
    requires = manifest.get("requires", ())
    if requires is None:
        requires = ()
    valid_requires = isinstance(requires, list | tuple) and all(
        isinstance(value, str) and value.strip() for value in requires
    )
    if not valid_requires:
        raise _manifest_load_error(SkillInventoryDiagnosticCode.MALFORMED_FRONTMATTER, skill_md)
    identity = SkillIdentity(namespace=namespace, manifest_name=manifest_name.strip())
    return identity, skill_md, tuple(value.strip() for value in requires)


def _manifest_load_error(code: SkillInventoryDiagnosticCode, skill_md: Path) -> SkillInventoryLoadError:
    return SkillInventoryLoadError(
        (SkillInventoryDiagnostic(code=code, subject=str(skill_md), source_paths=(skill_md,)),)
    )


def _resolve_reference(
    reference: str,
    by_name: Mapping[str, SkillIdentity],
    by_qualified_name: Mapping[str, SkillIdentity],
) -> SkillIdentity | None:
    return by_qualified_name.get(reference) if ":" in reference else by_name.get(reference)


def _normalize_manifest_name(name: str) -> str:
    return name.strip().casefold()


def _transitive_requires(
    root: SkillIdentity,
    direct_by_identity: Mapping[SkillIdentity, tuple[SkillIdentity, ...]],
) -> tuple[SkillIdentity, ...]:
    ordered: list[SkillIdentity] = []
    seen: set[SkillIdentity] = set()
    visiting: set[SkillIdentity] = set()

    def visit(identity: SkillIdentity) -> None:
        if identity in visiting:
            return
        visiting.add(identity)
        for dependency in direct_by_identity[identity]:
            visit(dependency)
            if dependency != root and dependency not in seen:
                seen.add(dependency)
                ordered.append(dependency)
        visiting.remove(identity)

    visit(root)
    return tuple(ordered)


def _embedded_closure(
    context: str,
    declarations: AgentSkillDeclarations,
    source_path: Path,
    resolution_index: _SkillResolutionIndex,
) -> EmbeddedSkillClosure:
    declared = tuple(
        resolved
        for reference in declarations.skills
        if (
            resolved := _resolve_reference(
                reference,
                resolution_index.by_name,
                resolution_index.by_qualified_name,
            )
        )
        is not None
    )
    companion_skills = tuple(
        resolved
        for reference in declarations.companion_skills
        if (
            resolved := _resolve_reference(
                reference,
                resolution_index.by_name,
                resolution_index.by_qualified_name,
            )
        )
        is not None
    )
    embedded = tuple(
        dict.fromkeys(
            dependency
            for identity in (*declared, *companion_skills)
            for dependency in (*resolution_index.transitive_by_identity[identity], identity)
        )
    )
    missing_references = tuple(
        SkillInventoryDiagnostic(
            code=code,
            subject=f"agent:{context}",
            reference=reference,
            source_paths=(source_path,),
        )
        for references, code in (
            (declarations.skills, SkillInventoryDiagnosticCode.MISSING_AGENT_SKILL),
            (declarations.companion_skills, SkillInventoryDiagnosticCode.MISSING_AGENT_COMPANION),
        )
        for reference in references
        if _resolve_reference(
            reference,
            resolution_index.by_name,
            resolution_index.by_qualified_name,
        )
        is None
    )
    return EmbeddedSkillClosure(
        context=context,
        declared=declared,
        companion_skills=companion_skills,
        embedded=embedded,
        missing_references=missing_references,
    )


def _dependency_cycles(
    direct_by_identity: Mapping[SkillIdentity, tuple[SkillIdentity, ...]],
) -> tuple[DependencyCycle, ...]:
    state: dict[SkillIdentity, int] = {}
    stack: list[SkillIdentity] = []
    cycles: set[tuple[SkillIdentity, ...]] = set()

    def visit(identity: SkillIdentity) -> None:
        state[identity] = 1
        stack.append(identity)
        for dependency in direct_by_identity[identity]:
            if dependency not in state:
                visit(dependency)
            elif state[dependency] == 1:
                members = tuple(stack[stack.index(dependency) :])
                first = min(range(len(members)), key=lambda index: members[index].qualified_name)
                cycles.add((*members[first:], *members[:first]))
        stack.pop()
        state[identity] = 2

    for identity in direct_by_identity:
        if identity not in state:
            visit(identity)
    return tuple(DependencyCycle(identities=cycle) for cycle in sorted(cycles))
