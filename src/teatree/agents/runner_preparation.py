"""Prompt, skill assurance, and options prepared before a headless turn opens."""

from dataclasses import dataclass
from pathlib import Path

from claude_agent_sdk import ClaudeAgentOptions

from teatree.agents._runner_env import DispatchCredential
from teatree.agents._runner_options import SpawnOverrides, _build_options, _turn_ceiling
from teatree.agents.compaction_guard import CompactionGuard
from teatree.agents.harness_dispatch import DispatchHarness
from teatree.agents.prompt import build_system_context, build_task_prompt, required_skill_delivery
from teatree.agents.reader_profile import is_reader_phase
from teatree.agents.skill_assurance import SkillAssurance, assess_skill_dispatch, recover_truncated_inline_skills
from teatree.agents.stage_skill_prompt import stage_skills_present
from teatree.core.models import Task
from teatree.skill_support.loading import SkillLoadingPolicy


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
    prompt = build_task_prompt(task, skills=skills, stage_skills=stage_skills)
    lifecycle_skill = SkillLoadingPolicy.lifecycle_for_phase(phase)
    system_context = build_system_context(
        task,
        skills=skills,
        lifecycle_skill=lifecycle_skill,
        stage_skills=stage_skills,
        handoff=handoff,
    )
    required_inline, required_explicit = required_skill_delivery(
        task.phase,
        skills,
        lifecycle_skill=lifecycle_skill,
        stage_skills=stage_skills_present(task, skills, configured=stage_skills),
    )
    required_inline, required_explicit, load_directive = recover_truncated_inline_skills(
        required_inline=required_inline,
        required_explicit=required_explicit,
        rendered_context=system_context,
        can_load=not is_reader_phase(phase),
    )
    if load_directive:
        prompt = f"{load_directive}\n\n{prompt}"
    assurance = assess_skill_dispatch(
        skills=skills,
        required_inline=required_inline,
        required_explicit=required_explicit,
        rendered_context=f"{system_context}\n{prompt}",
    )
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
            model_is_resolved=(dispatch.route_candidate_index is not None or dispatch.name == "codex_app_server"),
            harness_name=dispatch.name,
            effort=dispatch.effort,
        ),
    )
    return PreparedRun(prompt, options, guard, assurance)
