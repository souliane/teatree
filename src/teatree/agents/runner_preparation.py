"""Prompt, skill assurance, and options prepared before a headless turn opens."""

import logging
import shutil
from dataclasses import dataclass, replace
from functools import partial
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
from teatree.agents.harness import Harness
from teatree.agents.harness_dispatch import DispatchHarness
from teatree.agents.harness_registry import HarnessFallbackError, HarnessFallbackKind
from teatree.agents.prompt import (
    build_read_only_system_context,
    build_read_only_task_prompt,
    build_system_context,
    build_task_prompt,
    read_only_required_skills,
    required_skill_delivery,
)
from teatree.agents.read_only_venue_brief import ReadOnlyFacts, fit_read_only_context, read_only_system_context
from teatree.agents.runner_interruption import _record_failure
from teatree.agents.skill_assurance import (
    SkillAssurance,
    SkillDelivery,
    assess_skill_dispatch,
    recover_truncated_inline_skills,
    skill_application_line,
)
from teatree.agents.skill_bundle import configured_lifecycle_skill
from teatree.agents.skill_injection import _read_only_skill_text
from teatree.agents.stage_skill_prompt import stage_skills_present
from teatree.agents.usage_window import maybe_park_for_active_window, park_task_on_all_exhausted
from teatree.config import AgentHarnessProvider, cold_reader
from teatree.core.autocompact_advisory import native_context_window
from teatree.core.models import Task, TaskAttempt
from teatree.credential_config import AllTokensExhaustedError
from teatree.llm.credentials import CredentialError
from teatree.skill_support.loading import SkillLoadingPolicy
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


def _brief(task: Task, preflight: Preflight, *, lifecycle_skill: str, handoff: Path | None) -> tuple[str, str, str]:
    """The task prompt and system context; the read-only executor gets its facts inline or the run is refused."""
    skills, stage_skills = preflight.skills, preflight.stage_skills
    if preflight.dispatch.harness.capabilities.read_only_venue:
        prompt = build_read_only_task_prompt(task, ReadOnlyFacts.gather(task, handoff))
        context = build_read_only_system_context(
            task, skills=skills, lifecycle_skill=lifecycle_skill, stage_skills=stage_skills
        )
        context, continued_skills = fit_read_only_context(context)
        if continued_skills:
            prompt = f"Required skills continued from the system context:\n\n{continued_skills}\n\n{prompt}"
        return prompt, context, continued_skills
    prompt = build_task_prompt(task, skills=skills, stage_skills=stage_skills)
    context = build_system_context(
        task, skills=skills, lifecycle_skill=lifecycle_skill, stage_skills=stage_skills, handoff=handoff
    )
    return prompt, context, ""


def _native_compaction_env(env: dict[str, str] | None, dispatch: DispatchHarness) -> dict[str, str] | None:
    if dispatch.name != "claude_sdk":
        return env
    window_read = cold_reader.read_setting_confirmed("agent_model_context_windows")
    if not window_read.readable:
        msg = "agent_model_context_windows could not be read"
        raise RuntimeError(msg)
    configured = window_read.value.get(dispatch.model) if isinstance(window_read.value, dict) else None
    window = (
        configured
        if isinstance(configured, int) and not isinstance(configured, bool) and configured > 0
        else native_context_window(dispatch.model)
    )
    if window is None:
        return env
    child_env = dict(env or {})
    child_env["DISABLE_COMPACT"] = "0"
    child_env["DISABLE_AUTO_COMPACT"] = "0"
    child_env["CLAUDE_CODE_AUTO_COMPACT_WINDOW"] = str(window)
    child_env["CLAUDE_AUTOCOMPACT_PCT_OVERRIDE"] = str(100 * (window - 16_000) / window)
    return child_env


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
    dispatch = preflight.dispatch
    read_only_venue = dispatch.harness.capabilities.read_only_venue
    lifecycle_skill = (
        configured_lifecycle_skill(phase) if read_only_venue else SkillLoadingPolicy.lifecycle_for_phase(phase)
    )
    prompt, system_context, continued_skills = _brief(task, preflight, lifecycle_skill=lifecycle_skill, handoff=handoff)
    stage_present = stage_skills_present(task, skills, configured=preflight.stage_skills)
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
    load_directive = ""
    if not read_only_venue:
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
        rendered_context=SkillDelivery(
            f"{system_context}\n{prompt}", partial(_read_only_skill_text, sweep_skill_name=lifecycle_skill)
        )
        if read_only_venue
        else f"{system_context}\n{prompt}",
    )
    if read_only_venue:
        system_context = read_only_system_context(system_context)
        if continued_skills:
            prompt = prompt.replace(continued_skills, read_only_system_context(continued_skills, max_bytes=None), 1)
    if assurance["requested"]:
        prompt = f"{prompt}\n\n{skill_application_line(assurance['requested'])}"
    guard = CompactionGuard()
    options = _build_options(
        task,
        system_context,
        phase=phase,
        skills=skills,
        overrides=SpawnOverrides(
            env=_native_compaction_env(credential.env, dispatch),
            turn_ceiling=_turn_ceiling(dispatch.harness),
            handoff=handoff,
            compaction_guard=None,
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
