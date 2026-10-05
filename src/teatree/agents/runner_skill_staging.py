"""Skill staging and budget refusal before the agent harness opens."""

import logging
from collections.abc import Callable

from teatree.agents.runner_budget import TicketBudget
from teatree.agents.runner_interruption import _record_failure
from teatree.agents.skill_bundle import ArchitecturalReviewSkillMissingError
from teatree.core.models import Task, TaskAttempt

logger = logging.getLogger("teatree.agents.runner")


def stage_skills_or_refusal(
    task: Task,
    *,
    phase: str,
    stage_skills: Callable[[str], list[str]],
) -> list[str] | TaskAttempt:
    """Every reason to refuse before the harness, then the dispatch's stage skills.

    Both refusals precede harness resolution (souliane/teatree#2916): for a resumed
    pydantic_ai task, resolving the harness destructively pops the parked ancestor's
    thread, so a run that will never start must not reach it or the conversation is
    lost. The skills are resolved ONCE here and threaded into every consumer (#3206);
    re-resolving per prompt builder re-warns on a misconfigured skill and re-reads its
    SKILL.md path for nothing.
    """
    budget_breach = TicketBudget.from_settings().breach_reason(task.ticket)
    if budget_breach is not None:
        logger.warning("Refusing dispatch for task %s: %s", task.pk, budget_breach)
        return _record_failure(task, error=budget_breach)  # no-usage: refused on budget — no turn billed
    try:
        return stage_skills(phase)
    except ArchitecturalReviewSkillMissingError as exc:
        logger.warning("Refusing dispatch for task %s: %s", task.pk, exc)
        return _record_failure(task, error=str(exc))  # no-usage: the skills never staged, so nothing was dispatched
