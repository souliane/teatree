"""Skill staging and budget refusal before the agent harness opens."""

import logging
from collections.abc import Callable
from datetime import timedelta

from django.utils import timezone

from teatree.agents.runner_budget import TicketBudget
from teatree.agents.runner_interruption import _record_failure
from teatree.agents.skill_assurance import SkillDispatchError
from teatree.agents.skill_bundle import (
    ArchitecturalReviewSkillMissingError,
    resolve_executor_sweep_bundle,
    resolve_skill_bundle,
    stage_skills_for_dispatch,
)
from teatree.agents.usage_window import record_park
from teatree.core.models import LIMIT_PARKED_PREFIX, Task, TaskAttempt
from teatree.core.worktree.clone_paths import dispatch_detection_root
from teatree.skill_support.index import install_roots
from teatree.skill_support.pin_shadow import SkillPinRefusalError, SkillPinsUnreadableError
from teatree.types import SkillMetadata

logger = logging.getLogger("teatree.agents.runner")

_UNREADABLE_PINS_RECHECK = timedelta(seconds=300)


def park_if_worker_skill_roots_empty(task: Task, exc: SkillDispatchError) -> TaskAttempt | None:
    if not exc.assurance["missing"]:
        return None
    roots = install_roots()
    try:
        populated = any(root.exists() and (not root.is_dir() or any(root.iterdir())) for root in roots)
    except OSError:
        return None
    if populated:
        return None
    attempt = record_park(
        task,
        reason=f"{LIMIT_PARKED_PREFIX}skill_roots_empty: {exc}",
        not_before=timezone.now() + _UNREADABLE_PINS_RECHECK,
    )
    attempt.result = {**(attempt.result or {}), "skill_assurance": exc.assurance}
    attempt.save(update_fields=["result"])
    return attempt


def staged_skills_or_refusal(
    task: Task, *, phase: str, overlay_skill_metadata: SkillMetadata
) -> tuple[list[str], list[str]] | TaskAttempt:
    """The dispatch's ``(stage skills, bundle)``, or the recorded refusal that stops it before the harness."""

    def bundle(stage_skills: list[str]) -> list[str]:
        return resolve_skill_bundle(
            phase=phase,
            overlay_skill_metadata=overlay_skill_metadata,
            detection_root=dispatch_detection_root(task.ticket),
            stage_skills=stage_skills,
        )

    return stage_skills_or_refusal(task, phase=phase, stage_skills=stage_skills_for_dispatch, bundle=bundle)


def stage_skills_or_refusal(
    task: Task,
    *,
    phase: str,
    stage_skills: Callable[[str], list[str]],
    bundle: Callable[[list[str]], list[str]],
) -> tuple[list[str], list[str]] | TaskAttempt:
    """Every reason to refuse before the harness, then the dispatch's ``(stage skills, bundle)``.

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
        staged = stage_skills(phase)
        return staged, bundle(staged)
    except (ArchitecturalReviewSkillMissingError, SkillPinRefusalError) as exc:
        return _refused(task, exc)


def bundle_or_refusal(task: Task, bundle: Callable[[], list[str]]) -> list[str] | TaskAttempt:
    """*bundle*'s skills, or the recorded refusal the declared skill pins earn in every dispatch lane."""
    try:
        return bundle()
    except SkillPinRefusalError as exc:
        return _refused(task, exc)


def read_only_sweep_skills_or_refusal(
    task: Task, overlay_skill_metadata: SkillMetadata, stage_skills: list[str]
) -> list[str] | TaskAttempt:
    return bundle_or_refusal(
        task,
        lambda: resolve_executor_sweep_bundle(
            overlay_skill_metadata=overlay_skill_metadata,
            detection_root=dispatch_detection_root(task.ticket),
            stage_skills=stage_skills,
        ),
    )


def _refused(task: Task, exc: Exception) -> TaskAttempt:
    logger.warning("Refusing dispatch for task %s: %s", task.pk, exc)
    if isinstance(exc, SkillPinsUnreadableError):
        # A manifest a restore or redeploy brings back is a wait, not a failure; the marker keeps it off the budget.
        return record_park(
            task,
            reason=f"{LIMIT_PARKED_PREFIX}pins_unreadable: {exc}",
            not_before=timezone.now() + _UNREADABLE_PINS_RECHECK,
        )
    return _record_failure(task, error=str(exc))  # no-usage: the skills never staged, so nothing was dispatched
