from dataclasses import dataclass, replace
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING

from django.shortcuts import redirect, render
from django.views.decorators.http import require_GET, require_POST

from teatree.config import get_effective_settings
from teatree.core.modelkit.phases import SUBAGENT_BY_PHASE
from teatree.core.models import ConfigSetting
from teatree.core.skill_sources import skill_demands_by_overlay
from teatree.dash import audit
from teatree.dash.skill_control import (
    SkillDashboard,
    SkillDashboardSource,
    build_skill_dashboard,
    filter_skill_dashboard,
    load_cached_receipt,
)
from teatree.dash.skills import recent_skill_assurance
from teatree.dash.views.access import require_loopback_or_staff
from teatree.dash.views.base import actor, error_page, nav_context
from teatree.harness_skills import HarnessSkillPolicyError, SkillsHarness
from teatree.paths import get_data_dir
from teatree.provisioning.declared import (
    DeclaredDependency,
    project_root_for_running_code,
    skills_declared_in_apm_manifest,
)
from teatree.provisioning.harness_skill_removal import (
    HarnessSkillRemovalError,
    HarnessSkillRemovalService,
    HarnessSkillRequirements,
    file_operation_lock,
)
from teatree.provisioning.skills_cli import (
    SkillsCli,
    SkillsCliError,
    SkillsInventoryReceiptError,
    refresh_inventory_receipt,
)
from teatree.skill_support.agent_declarations import default_agents_dir
from teatree.skill_support.demands import SkillDemand
from teatree.skill_support.inventory import SkillInventory
from teatree.skill_support.loading import DEFAULT_SKILLS_DIR

if TYPE_CHECKING:
    from django.http import HttpRequest, HttpResponse


@dataclass(frozen=True, slots=True)
class DashboardState:
    dashboard: SkillDashboard
    inventory: SkillInventory
    apm_dependencies: tuple[DeclaredDependency, ...]
    runtime_demands: tuple[SkillDemand, ...]
    exclusions: list[str]


@dataclass(frozen=True, slots=True)
class _RenderOptions:
    operation_error: str = ""
    status: int = 200
    nav_collision_count: int | None = None
    query: str = ""
    selected_status: str = ""


def _receipt_path() -> Path:
    return get_data_dir("skills") / "inventory.json"


def _persist_exclusions(exclusions: list[str]) -> None:
    value: list[object] = list(exclusions)
    ConfigSetting.objects.set_value("harness_skill_exclusions", value)


def _current_exclusions() -> list[str]:
    return get_effective_settings().harness_skill_exclusions


def _load_state() -> DashboardState:
    inventory = SkillInventory.load(
        namespace="t3",
        skills_dir=DEFAULT_SKILLS_DIR,
        agents_dir=default_agents_dir(),
        phase_agents={
            phase: agent.removeprefix("t3:")
            for (_role, phase), agent in SUBAGENT_BY_PHASE.items()
            if agent.startswith("t3:")
        },
    )
    root = project_root_for_running_code()
    dependencies = tuple(skills_declared_in_apm_manifest(root / "apm.yml")) if root is not None else ()
    demands = tuple(demand for overlay_demands in skill_demands_by_overlay().values() for demand in overlay_demands)
    exclusions = get_effective_settings().harness_skill_exclusions
    cached = load_cached_receipt(_receipt_path())
    dashboard = build_skill_dashboard(
        source=SkillDashboardSource(inventory, dependencies, demands, exclusions),
        receipt=cached.receipt,
        receipt_error=cached.error,
        now=datetime.now(UTC),
    )
    return DashboardState(dashboard, inventory, dependencies, demands, exclusions)


def _render(
    request: "HttpRequest",
    state: DashboardState,
    options: _RenderOptions | None = None,
) -> "HttpResponse":
    render_options = options or _RenderOptions()
    collision_count = (
        state.dashboard.collision_count
        if render_options.nav_collision_count is None
        else render_options.nav_collision_count
    )
    context = {
        **nav_context("dash:skills", skills_collision_count=collision_count),
        "inventory": state.dashboard,
        "operation_error": render_options.operation_error,
        "skill_query": render_options.query,
        "skill_status": render_options.selected_status,
        "recent_assurance": recent_skill_assurance(),
    }
    return render(request, "dash/skills.html", context, status=render_options.status)


@require_loopback_or_staff
@require_GET
def skills(request: "HttpRequest") -> "HttpResponse":
    state = _load_state()
    query = request.GET.get("q", "")
    selected_status = request.GET.get("status", "")
    filtered = filter_skill_dashboard(state.dashboard, query=query, status=selected_status)
    return _render(
        request,
        replace(state, dashboard=filtered),
        _RenderOptions(
            nav_collision_count=state.dashboard.collision_count,
            query=query,
            selected_status=selected_status,
        ),
    )


@require_loopback_or_staff
@require_POST
def skills_refresh(request: "HttpRequest") -> "HttpResponse":
    try:
        refresh_inventory_receipt(_receipt_path())
    except (OSError, SkillsCliError, SkillsInventoryReceiptError) as error:
        audit.record(actor=actor(request), action="skills:refresh-failed")
        return _render(request, _load_state(), _RenderOptions(operation_error=str(error), status=502))
    audit.record(actor=actor(request), action="skills:refresh")
    return redirect("dash:skills")


@require_loopback_or_staff
@require_POST
def skills_remove(request: "HttpRequest", harness: str, name: str) -> "HttpResponse":
    state = _load_state()
    try:
        selected_harness = SkillsHarness(harness)
    except ValueError:
        return error_page(request, "That harness is not supported.", back="dash:skills")
    group = next(item for item in state.dashboard.harnesses if item.harness is selected_harness)
    row = next((skill for skill in group.skills if skill.name == name), None)
    if row is None:
        return error_page(request, "That exact skill is not installed for this harness.", back="dash:skills")
    if not row.removable:
        return error_page(
            request,
            f"{name} is read-only because TeaTree requires it: {row.removal_reason}.",
            back="dash:skills",
        )
    first_party = {declaration.identity.manifest_name.casefold() for declaration in state.inventory.declarations}
    dependencies = tuple(
        dependency for dependency in state.apm_dependencies if dependency.name.casefold() not in first_party
    )
    demands = tuple(
        demand.name
        for demand in state.runtime_demands
        if demand.name.rsplit(":", 1)[-1].strip().casefold() not in first_party
    )
    service = HarnessSkillRemovalService(
        cli=SkillsCli(),
        persist_exclusions=_persist_exclusions,
        refresh_receipt=lambda: refresh_inventory_receipt(_receipt_path()),
        operation_lock=lambda: file_operation_lock(_receipt_path().parent / ".remove.lock"),
    )
    try:
        service.remove_optional(
            selected_harness,
            name,
            load_current_exclusions=_current_exclusions,
            requirements=HarnessSkillRequirements(state.inventory, dependencies, demands),
        )
    except HarnessSkillRemovalError as error:
        audit.record(actor=actor(request), action="skills:remove-failed", target=f"{harness}:{name}")
        return _render(request, state, _RenderOptions(operation_error=str(error), status=502))
    except HarnessSkillPolicyError as error:
        return error_page(request, str(error), back="dash:skills")
    audit.record(actor=actor(request), action="skills:remove", target=f"{harness}:{name}")
    return redirect("dash:skills")
