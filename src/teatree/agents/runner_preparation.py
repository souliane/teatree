"""Prompt, skill assurance, and options prepared before a headless turn opens."""

import logging
import shutil
from dataclasses import dataclass, replace
from pathlib import Path

from claude_agent_sdk import ClaudeAgentOptions

from teatree.agents._runner_env import (
    DispatchCredential,
    _overlay_scope,
    _provider_child_env,
    with_routed_github_token,
    with_test_worker_cap,
)
from teatree.agents._runner_options import SpawnOverrides, _build_options, _turn_ceiling
from teatree.agents.compaction_guard import CompactionGuard
from teatree.agents.dispatch_refusal import pre_harness_refusal
from teatree.agents.harness import Harness
from teatree.agents.harness_dispatch import DispatchHarness, resolve_dispatch_harness
from teatree.agents.harness_registry import (
    HarnessFallbackError,
    HarnessFallbackKind,
    InvalidHarnessProviderError,
    NoAvailableHarnessError,
    UnknownHarnessError,
)
from teatree.agents.prompt import (
    build_read_only_system_context,
    build_read_only_task_prompt,
    build_system_context,
    build_task_prompt,
    read_only_required_skills,
    required_skill_delivery,
)
from teatree.agents.read_only_venue_brief import ReadOnlyFacts, read_only_system_context
from teatree.agents.runner_interruption import _record_failure
from teatree.agents.runner_skill_staging import staged_skills_or_refusal
from teatree.agents.skill_assurance import SkillAssurance, assess_skill_dispatch, recover_truncated_inline_skills
from teatree.agents.skill_routing import AmbiguousSkillRouteError, ConflictingHarnessRoutingError
from teatree.agents.stage_skill_prompt import stage_skills_present
from teatree.agents.usage_window import (
    maybe_park_for_active_window,
    park_on_timed_rejections,
    park_task_on_all_exhausted,
)
from teatree.config import AgentHarnessProvider
from teatree.config.agent_spawn import InvalidAgentConfigError
from teatree.core.models import Task, TaskAttempt
from teatree.credential_config import AllTokensExhaustedError
from teatree.llm.credentials import CredentialError
from teatree.skill_support.loading import SkillLoadingPolicy
from teatree.types import SkillMetadata
from teatree.utils.agent_spawn_marker import dispatched_agent_env

logger = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class PreparedRun:
    prompt: str
    options: ClaudeAgentOptions
    compaction_guard: CompactionGuard
    skill_assurance: SkillAssurance


@dataclass(frozen=True, slots=True)
class Preflight:
    """Stage skills, dispatch backend, and bundle admitted before harness open."""

    stage_skills: list[str]
    dispatch: DispatchHarness
    skills: list[str]


def restore_unconsumed_resume_thread(harness: Harness) -> None:
    restore = getattr(harness, "restore_unconsumed_resume_thread", None)
    if callable(restore):
        restore()


def _brief(task: Task, preflight: Preflight, *, lifecycle_skill: str, handoff: Path | None) -> tuple[str, str]:
    """The task prompt and system context; the read-only executor gets its facts inline or the run is refused."""
    skills, stage_skills = preflight.skills, preflight.stage_skills
    if preflight.dispatch.harness.capabilities.read_only_venue:
        prompt = build_read_only_task_prompt(task, ReadOnlyFacts.gather(task, handoff))
        context = build_read_only_system_context(
            task, skills=skills, lifecycle_skill=lifecycle_skill, stage_skills=stage_skills
        )
        return prompt, context
    prompt = build_task_prompt(task, skills=skills, stage_skills=stage_skills)
    context = build_system_context(
        task, skills=skills, lifecycle_skill=lifecycle_skill, stage_skills=stage_skills, handoff=handoff
    )
    return prompt, context


def prepare_run(
    task: Task,
    preflight: Preflight,
    *,
    phase: str,
    handoff: Path | None,
    credential: DispatchCredential,
) -> PreparedRun:
    """Refuse an undelivered mandatory skill before creating billable options."""
    skills = preflight.skills
    stage_skills = preflight.stage_skills
    dispatch = preflight.dispatch
    lifecycle_skill = SkillLoadingPolicy.lifecycle_for_phase(phase)
    prompt, system_context = _brief(task, preflight, lifecycle_skill=lifecycle_skill, handoff=handoff)
    read_only_venue = dispatch.harness.capabilities.read_only_venue
    stage_present = stage_skills_present(task, skills, configured=stage_skills)
    if read_only_venue:
        handoff = None
        required_inline = read_only_required_skills(
            task.phase, skills, lifecycle_skill=lifecycle_skill, stage_skills=stage_present
        )
        required_explicit: set[str] = set()
    else:
        required_inline, required_explicit = required_skill_delivery(
            task.phase, skills, lifecycle_skill=lifecycle_skill, stage_skills=stage_present
        )
    required_inline, required_explicit, load_directive = recover_truncated_inline_skills(
        required_inline=required_inline,
        required_explicit=required_explicit,
        rendered_context=system_context,
    )
    if load_directive:
        prompt = f"{load_directive}\n\n{prompt}"
    assurance = assess_skill_dispatch(
        skills=skills,
        required_inline=required_inline,
        required_explicit=required_explicit,
        rendered_context=f"{system_context}\n{prompt}",
    )
    if read_only_venue:
        system_context = read_only_system_context(system_context)
    guard = CompactionGuard()
    options = _build_options(
        task,
        system_context,
        phase=phase,
        skills=skills,
        overrides=SpawnOverrides(
            env=credential.env,
            turn_ceiling=_turn_ceiling(dispatch.harness),
            handoff=handoff,
            compaction_guard=guard if dispatch.harness.capabilities.spawns_cli_child else None,
            model=dispatch.model,
            spawn_selection=dispatch.spawn_selection,
            model_is_resolved=(dispatch.route_candidate_index is not None or dispatch.name == "codex_app_server"),
            harness_name=dispatch.name,
            mcp=dispatch.harness.capabilities.mcp,
            effort=dispatch.effort,
        ),
    )
    return PreparedRun(prompt, options, guard, assurance)


@dataclass(frozen=True, slots=True)
class _ChildEnvDispatch:
    lane: str = ""
    phase: str = ""
    route_candidate: bool = False


_DEFAULT_CHILD_ENV_DISPATCH = _ChildEnvDispatch()


def _preflight(
    task: Task,
    *,
    phase: str,
    overlay_skill_metadata: SkillMetadata,
) -> Preflight | TaskAttempt:
    """The refusals that must all pass before a turn can be billed, as one short-circuit."""
    refusal = pre_harness_refusal(task, phase=phase)
    if refusal is not None:
        logger.warning("Refusing dispatch for task %s: %s", task.pk, refusal)
        return _record_failure(task, error=refusal)  # no-usage: refused before the harness opened — no turn billed

    staged = staged_skills_or_refusal(task, phase=phase, overlay_skill_metadata=overlay_skill_metadata)
    if isinstance(staged, TaskAttempt):
        return staged
    stage_skills, skills = staged
    dispatch = _resolve_backend_or_failure(task, phase=phase, skills=skills)
    if isinstance(dispatch, TaskAttempt):
        return dispatch

    return Preflight(stage_skills=stage_skills, dispatch=dispatch, skills=skills)


def _resolve_backend_or_failure(
    task: Task, *, phase: str = "", skills: list[str] | None = None
) -> DispatchHarness | TaskAttempt:
    """Resolve the headless transport ONCE, or a recorded failure for an unresolvable backend."""
    try:
        return resolve_dispatch_harness(task, phase=phase or None, skills=skills)
    except NoAvailableHarnessError as exc:
        parked = park_on_timed_rejections(task, exc.rejected)
        return parked or _record_failure(task, error=str(exc))  # no-usage: no candidate opened, so no turn billed
    except (
        NotImplementedError,
        UnknownHarnessError,
        InvalidHarnessProviderError,
        CredentialError,
        AmbiguousSkillRouteError,
        ConflictingHarnessRoutingError,
        InvalidAgentConfigError,
    ) as exc:
        return _record_failure(task, error=str(exc))  # no-usage: an unimplemented/unresolvable backend never ran


def _admission_park_or_child_env(
    task: Task,
    harness: Harness,
    provider: AgentHarnessProvider | None,
    *,
    context: _ChildEnvDispatch = _DEFAULT_CHILD_ENV_DISPATCH,
) -> DispatchCredential | TaskAttempt:
    """Directive #3 admission guard, then the child-env resolution — one early-return seam."""
    admission_park = maybe_park_for_active_window(task, lane=context.lane)
    if admission_park is not None:
        restore_unconsumed_resume_thread(harness)
        return admission_park
    return _resolve_child_env_or_failure(task, harness, provider, context=context)


def _resolve_child_env_or_failure(
    task: Task,
    harness: Harness,
    provider: AgentHarnessProvider | None,
    *,
    context: _ChildEnvDispatch = _DEFAULT_CHILD_ENV_DISPATCH,
) -> DispatchCredential | TaskAttempt:
    """Resolve the ``claude`` CLI child env for a :class:`~teatree.agents.harness.ClaudeSdkHarness` dispatch."""
    if not harness.capabilities.spawns_cli_child:
        return DispatchCredential(env=_capped_forge_env(task, None) if harness.capabilities.managed_lane else None)
    # The SDK spawns the ``claude`` CLI child; keep the same provisioning gate
    # the ``claude -p`` runner used.
    if shutil.which("claude") is None:
        if context.route_candidate:
            message = "claude is not installed"
            raise HarnessFallbackError(message, kind=HarnessFallbackKind.TRANSPORT)
        return _record_failure(
            task, error="claude is not installed"
        )  # no-usage: the CLI is absent, so nothing was dispatched
    try:
        resolved = _provider_child_env(provider, scope=_overlay_scope(task))
    except CredentialError as exc:
        # #C2: every configured account drained (an ``AllTokensExhaustedError``) → quiesce the
        # lane and auto-resume at the earliest reset rather than escalating to a human; any
        # other credential gap (or flag-off) records the loud terminal FAILED as before.
        if isinstance(exc, AllTokensExhaustedError):
            parked = park_task_on_all_exhausted(task, resets_at=exc.earliest_reset, lane=context.lane)
            if parked is not None:
                return parked
        if context.route_candidate:
            kind = HarnessFallbackKind.QUOTA if isinstance(exc, AllTokensExhaustedError) else HarnessFallbackKind.AUTH
            raise HarnessFallbackError(str(exc), kind=kind) from exc
        logger.warning("Refusing dispatch for task %s: %s", task.pk, exc)
        return _record_failure(task, error=str(exc))  # no-usage: the credential gap is pre-dispatch — no turn billed
    return replace(resolved, env=_capped_forge_env(task, resolved.env))


def _capped_forge_env(task: Task, env: dict[str, str] | None) -> dict[str, str]:
    capped = with_test_worker_cap(env, active_agents=_active_agent_count())
    return {**(with_routed_github_token(capped, overlay=_overlay_scope(task)) or {}), **dispatched_agent_env()}


def _active_agent_count() -> int:
    """Live headless agents in flight — the divisor for the test-worker budget (#3644/F9)."""
    return max(1, Task.objects.claimed_agent_count())
