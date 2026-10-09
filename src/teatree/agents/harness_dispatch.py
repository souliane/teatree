"""Per-dispatch harness selection, ordered skill routes, and provider binding."""

import logging
from dataclasses import dataclass, replace
from typing import TYPE_CHECKING

from teatree.agents.credential_policy import resolve_credential_provider
from teatree.agents.harness_registry import (
    HarnessBuildContext,
    HarnessCapabilities,
    HarnessRejection,
    HarnessSelection,
    HarnessSpec,
    NoAvailableHarnessError,
    UnknownHarnessError,
    assert_provider_valid_for_harness,
    resolve_harness_spec,
    select_harness,
    valid_providers_for,
)
from teatree.agents.model_tiering import (
    HARNESS_EFFORT_SCALE,
    PHASE_HARNESS,
    SpawnModelSelection,
    _resolve_spawn_model_selection,
    resolve_phase_harness,
)
from teatree.agents.sdk_tool_map import phase_bars_write_tools
from teatree.agents.session_lineage import honesty_subject
from teatree.agents.skill_routing import resolve_skill_route
from teatree.config import AgentHarnessProvider, get_effective_settings
from teatree.config.agent_spawn import EFFORT_SCALE, AgentRouteCandidate, resolve_agent_config
from teatree.core.cost import tier_rank
from teatree.core.modelkit.phases import normalize_phase
from teatree.core.overlay_loader import OverlayConfigResolver
from teatree.llm.credentials import CredentialError
from teatree.skill_support.loading import SkillLoadingPolicy

if TYPE_CHECKING:
    from teatree.agents.harness import Harness
    from teatree.config import UserSettings
    from teatree.core.models import Task, UsageWindowState

logger = logging.getLogger(__name__)

MANAGED_CHATGPT_PROVIDER = "managed_chatgpt"


def _route_effort_unavailable_reason(effort: str | None, harness: str) -> str | None:
    if effort is None or effort in HARNESS_EFFORT_SCALE.get(harness, EFFORT_SCALE):
        return None
    return f"effort {effort!r} is unsupported by harness {harness!r}"


@dataclass(frozen=True, slots=True)
class SkillRouteSelection:
    source_skill: str
    index: int
    candidate: AgentRouteCandidate
    selection: HarnessSelection
    provider: AgentHarnessProvider | None


@dataclass(frozen=True, slots=True)
class DispatchHarness:
    """One resolved transport and the credential/model route that built it."""

    harness: "Harness"
    name: str
    provider: AgentHarnessProvider | None
    model: str | None = None
    spawn_selection: SpawnModelSelection | None = None
    effort: str | None = None
    route_source_skill: str = ""
    route_candidate_index: int | None = None
    rejected: tuple[HarnessRejection, ...] = ()
    availability_provider: str = ""


def task_overlay(task: "Task | None") -> str | None:
    if task is None:
        return None
    return task.ticket.overlay or None


def overlay_phase_candidates(task: "Task | None", phase: str | None) -> list[str]:
    """Return the ordered candidates declared by the task overlay for *phase*."""
    if task is None or phase is None or not task.ticket.overlay or not task.ticket.has_dispatchable_overlay():
        return []
    declared = OverlayConfigResolver.factory_phase_harness_candidates(task.ticket.overlay)
    for key, names in declared.items():
        if normalize_phase(key) == normalize_phase(phase) and isinstance(names, list):
            return [name for name in names if isinstance(name, str) and name]
    return []


def candidate_selection(context: HarnessBuildContext, candidates: list[str]) -> HarnessSelection | None:
    if not candidates:
        return None
    pinned = dict.fromkeys(resolve_phase_harness(candidate, context.phase) for candidate in candidates)
    return select_harness(list(pinned), context)


def _route_lane(capabilities: HarnessCapabilities, provider: AgentHarnessProvider | None) -> str:
    from teatree.core.models import TaskAttempt  # noqa: PLC0415 — deferred Django model import

    if capabilities.managed_lane:
        return TaskAttempt.Lane.MANAGED
    if capabilities.metered_lane or provider in {
        AgentHarnessProvider.API_KEY,
        AgentHarnessProvider.OPENAI_COMPATIBLE,
        AgentHarnessProvider.ANTHROPIC_API,
    }:
        return TaskAttempt.Lane.METERED
    if provider is AgentHarnessProvider.SUBSCRIPTION_OAUTH:
        return TaskAttempt.Lane.SUBSCRIPTION
    return ""


def _active_window(lane: str) -> "UsageWindowState | None":
    from django.utils import timezone  # noqa: PLC0415 — deferred Django import

    from teatree.core.models import UsageWindowState  # noqa: PLC0415 — deferred Django model import

    window = UsageWindowState.objects.active_for_lane(lane)
    return None if window is None or window.should_clear(timezone.now()) else window


def another_lane_can_dispatch(parked_lane: str) -> bool:
    """Whether the configured harness or a global route candidate could dispatch on a lane other than *parked_lane*."""
    settings = get_effective_settings(None)
    routes = [c for policy in resolve_agent_config().skill_models.values() if isinstance(policy, tuple) for c in policy]
    candidates = [AgentRouteCandidate(settings.agent_harness, ""), *routes]
    return any(_dispatchable_off_lane(settings, candidate, parked_lane) for candidate in candidates)


def _dispatchable_off_lane(settings: "UserSettings", candidate: AgentRouteCandidate, parked_lane: str) -> bool:
    try:
        spec = resolve_harness_spec(candidate.harness)
    except UnknownHarnessError:
        return False
    provider = AgentHarnessProvider.parse(candidate.provider) if candidate.provider else settings.agent_harness_provider
    lane = _route_lane(spec.capabilities, provider if spec.allows_provider else None)
    if lane == parked_lane or _active_window(lane) is not None:
        return False
    return spec.unavailable_reason(HarnessBuildContext(settings=settings, model=candidate.model)) is None


def _registered_route_spec(
    context: HarnessBuildContext, candidate: AgentRouteCandidate
) -> tuple[HarnessSpec | None, tuple[HarnessRejection, ...]]:
    try:
        return resolve_harness_spec(candidate.harness), ()
    except UnknownHarnessError:
        try:
            select_harness(
                [candidate.harness],
                replace(context, model=candidate.model, provider=candidate.provider or ""),
            )
        except NoAvailableHarnessError as exc:
            return None, exc.rejected
        return None, ()


def provider_under(settings: "UserSettings", name: str, phase: str | None) -> AgentHarnessProvider | None:
    """Resolve the provider that remains valid after a phase/overlay harness flip."""
    provider = settings.agent_harness_provider
    if provider is None or name == settings.agent_harness:
        return provider
    valid = valid_providers_for(name)
    if valid and provider.value not in valid:
        logger.warning(
            "phase=%s runs on agent_harness=%s, under which the configured "
            "agent_harness_provider=%s is not valid; this dispatch drops the Layer-2 pin and "
            "uses the ambient credential (the configured agent_harness=%s is unaffected)",
            phase,
            name,
            provider.value,
            settings.agent_harness,
        )
        return None
    return provider


def _route_provider(
    context: HarnessBuildContext,
    candidate: AgentRouteCandidate,
    spec: HarnessSpec,
) -> AgentHarnessProvider | None:
    provider = AgentHarnessProvider.parse(candidate.provider) if candidate.provider else None
    if provider is None and spec.allows_provider and context.settings is not None:
        provider = provider_under(context.settings, candidate.harness, context.phase)
    assert_provider_valid_for_harness(candidate.harness, provider.value if provider is not None else None)
    if spec.allows_provider:
        provider = resolve_credential_provider(provider, scope=context.overlay)
    return provider


@dataclass(frozen=True, slots=True)
class _EligibleCandidate:
    selection: HarnessSelection
    provider: AgentHarnessProvider | None


def _managed_provider_name(spec: HarnessSpec, provider: AgentHarnessProvider | None) -> str:
    if provider is not None:
        return provider.value
    return MANAGED_CHATGPT_PROVIDER if spec.capabilities.managed_lane else ""


def _static_rejection(context: HarnessBuildContext, candidate: AgentRouteCandidate, floor_rank: int) -> str | None:
    if reason := _route_effort_unavailable_reason(candidate.effort, candidate.harness):
        return reason
    pinned = resolve_phase_harness(candidate.harness, context.phase)
    if context.phase in PHASE_HARNESS and pinned != candidate.harness:
        return f"phase {context.phase!r} is pinned to harness {pinned!r}"
    if tier_rank(candidate.tier or candidate.model) < floor_rank:
        return "model is below a loaded skill's scalar floor"
    return None


def _eligible_candidate(
    context: HarnessBuildContext, candidate: AgentRouteCandidate
) -> _EligibleCandidate | tuple[HarnessRejection, ...]:
    spec, registration_rejections = _registered_route_spec(context, candidate)
    if spec is None:
        return registration_rejections
    try:
        provider = _route_provider(context, candidate, spec)
    except (CredentialError, ValueError) as exc:
        return (HarnessRejection(candidate.harness, str(exc)),)
    if window := _active_window(_route_lane(spec.capabilities, provider)):
        reason = f"{window.cause or 'quota'} window on lane {window.lane or 'ambient'!r} is active"
        return (HarnessRejection(candidate.harness, reason, retry_at=window.resets_at),)
    candidate_context = replace(context, model=candidate.model, provider=_managed_provider_name(spec, provider))
    try:
        return _EligibleCandidate(select_harness([candidate.harness], candidate_context), provider)
    except NoAvailableHarnessError as exc:
        return exc.rejected


def _skill_route_selection(
    context: HarnessBuildContext,
    *,
    skills: list[str],
    phase_candidates: list[str],
) -> SkillRouteSelection | None:
    config = resolve_agent_config(context.overlay)
    primary_skill = SkillLoadingPolicy.lifecycle_for_phase(context.phase or "")
    route = resolve_skill_route(
        config.skill_models, skills, phase_candidates=phase_candidates, primary_skill=primary_skill
    )
    if route is None:
        return None
    source_skill, candidates = route
    if context.phase and source_skill != primary_skill and phase_bars_write_tools(context.phase):
        return None
    failed = context.task.attempts.failed_route_candidates(source_skill) if context.task is not None else {}
    scalar_floors = [floor for skill in skills if isinstance((floor := config.skill_models.get(skill)), str)]
    floor_rank = max((tier_rank(floor) for floor in scalar_floors), default=-1)
    rejected: list[HarnessRejection] = []
    retry: SkillRouteSelection | None = None
    for index, candidate in enumerate(candidates):
        if reason := _static_rejection(context, candidate, floor_rank):
            rejected.append(HarnessRejection(candidate.harness, reason))
            continue
        eligible = _eligible_candidate(context, candidate)
        if not isinstance(eligible, _EligibleCandidate):
            rejected.extend(eligible)
            continue
        passing = SkillRouteSelection(
            source_skill,
            index,
            candidate,
            HarnessSelection(eligible.selection.spec, tuple(rejected)),
            eligible.provider,
        )
        if index not in failed:
            return passing
        retry = passing
        rejected.append(HarnessRejection(candidate.harness, f"attempt {failed[index]} failed on this candidate"))
    if retry is not None:
        return retry
    raise NoAvailableHarnessError(tuple(rejected))


def resolve_dispatch_harness(
    task: "Task | None" = None,
    *,
    phase: str | None = None,
    skills: list[str] | None = None,
) -> DispatchHarness:
    """Resolve, build, and credential a dispatch from one stable selection."""
    overlay = task_overlay(task)
    settings = get_effective_settings(overlay)
    configured = settings.agent_harness_provider
    context = HarnessBuildContext(task=task, phase=phase, settings=settings, overlay=overlay or "")
    phase_candidates = overlay_phase_candidates(task, phase)
    route = _skill_route_selection(context, skills=skills or [], phase_candidates=phase_candidates)
    if route is None:
        assert_provider_valid_for_harness(
            settings.agent_harness,
            configured.value if configured is not None else None,
        )
    selection = candidate_selection(context, phase_candidates) if route is None else route.selection
    if route is not None:
        name, spec = route.candidate.harness, route.selection.spec
        provider = route.provider
        candidate_context = replace(
            context,
            model=route.candidate.model,
            tier=route.candidate.tier,
            provider=_managed_provider_name(spec, provider),
        )
        return DispatchHarness(
            harness=spec.factory(candidate_context),
            name=name,
            provider=provider,
            model=route.candidate.model,
            effort=route.candidate.effort,
            route_source_skill=route.source_skill,
            route_candidate_index=route.index,
            rejected=route.selection.rejected,
            availability_provider=candidate_context.provider,
        )
    if selection is None:
        name = resolve_phase_harness(settings.agent_harness, phase)
        spec = resolve_harness_spec(name)
    else:
        name, spec = selection.spec.name, selection.spec
        if selection.rejected:
            logger.warning(
                "phase=%s dispatches harness candidate %s after rejecting %s",
                phase,
                name,
                "; ".join(map(str, selection.rejected)),
            )
    provider = None
    if spec.allows_provider:
        provider = resolve_credential_provider(provider_under(settings, name, phase), scope=overlay or "")
    spawn_selection = None
    if phase is not None:
        subject = honesty_subject(task) if task is not None else None
        task_id = subject.task_id if subject else int(task.pk) if task is not None else None
        spawn_selection = _resolve_spawn_model_selection(
            phase,
            skills=skills or [],
            session_id=subject.session_id if subject else None,
            task_id=task_id,
        )
        context = replace(context, tier=spawn_selection.tier)
    return DispatchHarness(harness=spec.factory(context), name=name, provider=provider, spawn_selection=spawn_selection)
