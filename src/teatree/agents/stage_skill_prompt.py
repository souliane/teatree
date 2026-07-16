"""Scope and frame per-stage overlay skills for a headless dispatch prompt.

An overlay's ``stage_skills`` map declares ADDITIONAL skills per phase. A
no-Skill-tool maker agent (``t3:coder``/``t3:debugger``/``t3:tester``/``t3:e2e``/
``t3:shipper``) cannot load a skill by reference, so each configured stage skill
present in the resolved bundle embeds IN FULL, carrying one precedence line that
keeps the lifecycle/overlay base authoritative — additive, never replacing.
"""

from teatree.agents.skill_injection import _explicit_load_name
from teatree.core.models import Task


def stage_skills_present(task: Task, skills: list[str], *, configured: list[str] | None = None) -> list[str]:
    """The overlay's configured stage skills for *task*'s phase, present in *skills*.

    Only stage skills actually in the resolved bundle are scoped, so an
    unresolvable one is not falsely surfaced as embedded. *configured* threads
    the dispatch's single ``active_overlay_stage_skills`` resolution (#3206) so
    the prompt builders reuse it instead of re-resolving (which re-warns and
    re-reads SKILL.md); when absent, it is resolved here.
    """
    if configured is None:
        from teatree.agents.skill_bundle import (  # noqa: PLC0415 — deferred: call-time import
            active_overlay_stage_skills,
        )

        configured = active_overlay_stage_skills(task.phase)
    configured_set = set(configured)
    if not configured_set:
        return []
    return [s for s in skills if _explicit_load_name(s) in configured_set]


def stage_precedence_line(stage_skills: list[str]) -> str:
    """The one precedence line the embedded stage-skill block carries."""
    names = ", ".join(_explicit_load_name(s) for s in stage_skills)
    return (
        f"STAGE CUSTOM SKILLS (overlay-configured for this phase): {names}. "
        "These stage skills are ADDITIVE — apply them IN ADDITION to the lifecycle and "
        "overlay skills above; on any conflict, the t3 lifecycle skill and the overlay/base "
        "instructions are authoritative."
    )
