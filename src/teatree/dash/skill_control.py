from collections import defaultdict
from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass, replace
from datetime import datetime, timedelta
from pathlib import Path

from teatree.harness_skills import (
    HarnessSkillRequiredError,
    SkillsHarness,
    authorize_harness_skill_removal,
    parse_harness_skill_exclusions,
)
from teatree.provisioning.declared import DeclaredDependency
from teatree.provisioning.skill_provenance import SkillInstallationFact
from teatree.provisioning.skills_cli import (
    InstalledSkill,
    SkillsInventoryReceipt,
    SkillsInventoryReceiptError,
    read_inventory_receipt,
)
from teatree.skill_support.demands import SkillDemand
from teatree.skill_support.inventory import (
    EmbeddedSkillClosure,
    HarnessSkillInstallation,
    SkillDeclaration,
    SkillIdentity,
    SkillInventory,
)

_RECEIPT_MAX_AGE = timedelta(hours=24)


@dataclass(frozen=True, slots=True)
class CachedReceipt:
    receipt: SkillsInventoryReceipt | None
    error: str
    stale: bool


@dataclass(frozen=True, slots=True)
class ReceiptStatus:
    generated_at: datetime | None
    cli_version: str
    stale: bool
    error: str


@dataclass(frozen=True, slots=True)
class TeatreeSkillRow:
    identity: str
    manifest_name: str
    source_path: str
    declared_requires: tuple[str, ...]
    direct_requires: tuple[str, ...]
    transitive_requires: tuple[str, ...]
    direct_agents: tuple[str, ...]
    companion_agents: tuple[str, ...]
    embedded_agents: tuple[str, ...]
    embedded_phases: tuple[str, ...]
    actually_embedded: bool
    danger: bool
    collisions: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class RequiredSkillRow:
    name: str
    sources: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class HarnessSkillRow:
    harness: SkillsHarness
    name: str
    canonical_identity: str
    manager_path: str
    source: str
    source_url: str
    source_type: str
    installation: SkillInstallationFact | None
    excluded: bool
    required: bool
    removal_reason: str
    removable: bool
    danger: bool
    collisions: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class HarnessRows:
    harness: SkillsHarness
    label: str
    skills: tuple[HarnessSkillRow, ...]


@dataclass(frozen=True, slots=True)
class SkillDashboard:
    receipt: ReceiptStatus
    teatree_skills: tuple[TeatreeSkillRow, ...]
    required_external: tuple[RequiredSkillRow, ...]
    harnesses: tuple[HarnessRows, ...]
    diagnostics: tuple[str, ...]
    cycles: tuple[tuple[str, ...], ...]
    collision_count: int


@dataclass(frozen=True, slots=True)
class SkillDashboardSource:
    inventory: SkillInventory
    apm_dependencies: tuple[DeclaredDependency, ...]
    runtime_demands: tuple[SkillDemand, ...]
    exclusions: object


@dataclass(frozen=True, slots=True)
class _ProjectionContext:
    inventory: SkillInventory
    external_dependencies: tuple[DeclaredDependency, ...]
    external_demands: tuple[SkillDemand, ...]
    exclusions: set[str]
    collision_keys: set[tuple[str, str, str]]


def load_cached_receipt(path: Path) -> CachedReceipt:
    try:
        return CachedReceipt(receipt=read_inventory_receipt(path), error="", stale=False)
    except FileNotFoundError:
        return CachedReceipt(
            receipt=None,
            error="Refresh inventory to inspect installed harness skills.",
            stale=True,
        )
    except (OSError, SkillsInventoryReceiptError) as error:
        return CachedReceipt(receipt=None, error=str(error), stale=True)


def build_skill_dashboard(
    *,
    source: SkillDashboardSource,
    receipt: SkillsInventoryReceipt | None,
    receipt_error: str | None,
    now: datetime,
) -> SkillDashboard:
    inventory = source.inventory
    first_party_names = {declaration.identity.manifest_name.casefold() for declaration in inventory.declarations}
    external_dependencies = tuple(
        dependency for dependency in source.apm_dependencies if dependency.name.casefold() not in first_party_names
    )
    external_demands = tuple(
        demand for demand in source.runtime_demands if _bare_name(demand.name).casefold() not in first_party_names
    )
    normalized_exclusions = set(parse_harness_skill_exclusions(source.exclusions))
    receipt_skills = _receipt_skills(receipt)
    installations = tuple(
        HarnessSkillInstallation(
            harness=harness.value,
            identity=SkillIdentity(_installed_namespace(skill, harness), skill.name),
            source_path=Path(skill.installation.front_door_path if skill.installation is not None else skill.path),
        )
        for harness, skill in receipt_skills
    )
    collisions = inventory.same_name_hazards(installations)
    collision_keys = {
        (
            collision.harness_skill.harness,
            collision.harness_skill.identity.qualified_name,
            collision.teatree_skill.identity.qualified_name,
        )
        for collision in collisions
    }
    context = _ProjectionContext(
        inventory,
        external_dependencies,
        external_demands,
        normalized_exclusions,
        collision_keys,
    )
    teatree_rows = tuple(_teatree_row(declaration, inventory, collision_keys) for declaration in inventory.declarations)
    harnesses = tuple(
        HarnessRows(
            harness=harness,
            label="Claude Code" if harness is SkillsHarness.CLAUDE_CODE else "Codex",
            skills=tuple(
                _harness_row(
                    harness,
                    skill,
                    context,
                )
                for installed_harness, skill in receipt_skills
                if installed_harness is harness
            ),
        )
        for harness in SkillsHarness
    )
    return SkillDashboard(
        receipt=_receipt_status(receipt, receipt_error, now),
        teatree_skills=teatree_rows,
        required_external=_required_external(inventory, external_dependencies, external_demands),
        harnesses=harnesses,
        diagnostics=tuple(
            f"{diagnostic.code.value}: {diagnostic.subject} -> {diagnostic.reference}"
            for diagnostic in inventory.missing_references
        ),
        cycles=tuple(tuple(identity.qualified_name for identity in cycle.identities) for cycle in inventory.cycles),
        collision_count=len(collisions),
    )


def filter_skill_dashboard(dashboard: SkillDashboard, *, query: str, status: str) -> SkillDashboard:
    normalized_query = query.strip().casefold()
    normalized_status = status.strip().casefold()
    teatree_skills = tuple(
        row
        for row in dashboard.teatree_skills
        if _matches_query(normalized_query, row.identity, row.source_path, *row.collisions)
        and (normalized_status != "collision" or row.danger)
        and normalized_status != "update"
    )
    required_external = tuple(
        row
        for row in dashboard.required_external
        if _matches_query(normalized_query, row.name, *row.sources) and normalized_status not in {"collision", "update"}
    )
    harnesses = tuple(
        replace(
            group,
            skills=tuple(
                row
                for row in group.skills
                if _matches_query(
                    normalized_query,
                    row.name,
                    row.canonical_identity,
                    row.manager_path,
                    row.source,
                    row.source_url,
                    *row.collisions,
                )
                and _matches_status(normalized_status, row)
            ),
        )
        for group in dashboard.harnesses
    )
    return replace(
        dashboard,
        teatree_skills=teatree_skills,
        required_external=required_external,
        harnesses=harnesses,
        collision_count=sum(len(row.collisions) for group in harnesses for row in group.skills),
    )


def _matches_query(query: str, *values: str) -> bool:
    return not query or any(query in value.casefold() for value in values)


def _matches_status(status: str, row: HarnessSkillRow) -> bool:
    if status == "collision":
        return row.danger
    if status == "required":
        return row.required
    if status == "update":
        return bool(
            row.installation is not None and row.installation.git is not None and row.installation.git.newer_on_default
        )
    return True


def _receipt_skills(receipt: SkillsInventoryReceipt | None) -> tuple[tuple[SkillsHarness, InstalledSkill], ...]:
    if receipt is None:
        return ()
    return tuple((group.harness, skill) for group in receipt.inventories for skill in group.skills)


def _installed_namespace(skill: InstalledSkill, harness: SkillsHarness) -> str:
    return skill.source or skill.source_url or harness.value


def _bare_name(name: str) -> str:
    return name.rsplit(":", 1)[-1].strip()


def _teatree_row(
    declaration: SkillDeclaration,
    inventory: SkillInventory,
    collision_keys: set[tuple[str, str, str]],
) -> TeatreeSkillRow:
    identity = declaration.identity
    matching = tuple(
        f"{harness}:{harness_identity}"
        for harness, harness_identity, teatree_identity in sorted(collision_keys)
        if teatree_identity == identity.qualified_name
    )
    return TeatreeSkillRow(
        identity=identity.qualified_name,
        manifest_name=identity.manifest_name,
        source_path=str(declaration.source_path),
        declared_requires=declaration.declared_requires,
        direct_requires=tuple(required.qualified_name for required in declaration.direct_requires),
        transitive_requires=tuple(required.qualified_name for required in declaration.transitive_requires),
        direct_agents=_contexts_with(identity, inventory.agent_closures, lambda closure: closure.declared),
        companion_agents=_contexts_with(
            identity,
            inventory.agent_closures,
            lambda closure: closure.companion_skills,
        ),
        embedded_agents=_contexts_with(identity, inventory.agent_closures, lambda closure: closure.embedded),
        embedded_phases=_contexts_with(identity, inventory.phase_closures, lambda closure: closure.embedded),
        actually_embedded=any(identity in closure.embedded for closure in inventory.agent_closures),
        danger=bool(matching),
        collisions=matching,
    )


def _contexts_with(
    identity: SkillIdentity,
    closures: Iterable[EmbeddedSkillClosure],
    memberships: Callable[[EmbeddedSkillClosure], tuple[SkillIdentity, ...]],
) -> tuple[str, ...]:
    return tuple(sorted(closure.context for closure in closures if identity in memberships(closure)))


def _harness_row(
    harness: SkillsHarness,
    skill: InstalledSkill,
    context: _ProjectionContext,
) -> HarnessSkillRow:
    namespace = _installed_namespace(skill, harness)
    identity = SkillIdentity(namespace, skill.name).qualified_name
    matching = tuple(
        teatree_identity
        for collision_harness, harness_identity, teatree_identity in sorted(context.collision_keys)
        if collision_harness == harness.value and harness_identity == identity
    )
    required = False
    removal_reason = "Optional harness skill"
    try:
        authorize_harness_skill_removal(
            harness,
            skill.name,
            inventory=context.inventory,
            apm_dependencies=context.external_dependencies,
            runtime_demands=(demand.name for demand in context.external_demands),
        )
    except HarnessSkillRequiredError as error:
        required = True
        removal_reason = ", ".join(error.required_by)
    exclusion = f"{harness.value}:{skill.name.casefold()}"
    return HarnessSkillRow(
        harness=harness,
        name=skill.name,
        canonical_identity=identity,
        manager_path=skill.path,
        source=skill.source or "",
        source_url=skill.source_url or "",
        source_type=skill.source_type or "",
        installation=skill.installation,
        excluded=exclusion in context.exclusions,
        required=required,
        removal_reason=removal_reason,
        removable=not required,
        danger=bool(matching),
        collisions=matching,
    )


def _required_external(
    inventory: SkillInventory,
    dependencies: Sequence[DeclaredDependency],
    demands: Sequence[SkillDemand],
) -> tuple[RequiredSkillRow, ...]:
    sources: defaultdict[str, set[str]] = defaultdict(set)
    for diagnostic in inventory.missing_references:
        sources[_bare_name(diagnostic.reference).casefold()].add(diagnostic.code.value)
    for dependency in dependencies:
        sources[dependency.name.casefold()].add(dependency.declared_in)
    for demand in demands:
        sources[_bare_name(demand.name).casefold()].add(f"runtime:{demand.source}")
    return tuple(RequiredSkillRow(name, tuple(sorted(reasons))) for name, reasons in sorted(sources.items()))


def _receipt_status(
    receipt: SkillsInventoryReceipt | None,
    receipt_error: str | None,
    now: datetime,
) -> ReceiptStatus:
    if receipt is None:
        return ReceiptStatus(
            generated_at=None,
            cli_version="",
            stale=True,
            error=receipt_error or "Refresh inventory to inspect installed harness skills.",
        )
    return ReceiptStatus(
        generated_at=receipt.generated_at,
        cli_version=receipt.cli_version,
        stale=now - receipt.generated_at > _RECEIPT_MAX_AGE,
        error=receipt_error or "",
    )
