"""Recorded work iterations and audit observations for a ticket phase."""

from typing import TYPE_CHECKING, cast

from django.apps import apps

from teatree.core.modelkit.phases import normalize_phase, phase_spellings
from teatree.core.modelkit.task_failure_taxonomy import CANCELLED_PREFIX, LEASE_EXPIRED_PREFIX, PLAN_STALE_PREFIX
from teatree.core.models.usage_window_state import PARK_ERROR_PREFIXES
from teatree.core.repair_loop import STALL_REPEAT_THRESHOLD
from teatree.llm.anthropic_limits import recoverable_exhaustion_cause

if TYPE_CHECKING:
    from teatree.core.models.task import Task
    from teatree.core.models.task_attempt import TaskAttempt, TaskAttemptQuerySet


_AUDIT_PREFIXES = (CANCELLED_PREFIX, PLAN_STALE_PREFIX, LEASE_EXPIRED_PREFIX)


def _attempt_model() -> "type[TaskAttempt]":
    return cast("type[TaskAttempt]", apps.get_model("core", "TaskAttempt"))


def repair_observations(attempts: "TaskAttemptQuerySet") -> list["TaskAttempt"]:
    ordered = attempts.excluding_parks().order_by("pk")
    return [attempt for attempt in ordered if recoverable_exhaustion_cause(attempt.error) is None]


def _is_repair_work(error: str) -> bool:
    return not error.startswith(PARK_ERROR_PREFIXES + _AUDIT_PREFIXES) and recoverable_exhaustion_cause(error) is None


def repair_iterations(attempts: "TaskAttemptQuerySet") -> list["TaskAttempt"]:
    """Executed work attempts in a queryset, oldest first."""
    sources = attempts.excluding_parks()
    work = [attempt for attempt in sources.order_by("pk") if _is_repair_work(attempt.error)]
    successor_errors = sources.filter(fallback_attempts__isnull=False).values_list("pk", "fallback_attempts__error")
    superseded_ids = {source_id for source_id, error in successor_errors if _is_repair_work(error)}
    return [attempt for attempt in work if attempt.pk not in superseded_ids]


def _ticket_phase_attempts(ticket_id: int, phase: str) -> "TaskAttemptQuerySet":
    return _attempt_model().objects.filter(
        task__ticket_id=ticket_id,
        task__phase__in=phase_spellings(normalize_phase(phase)),
    )


def ticket_phase_iterations(ticket_id: int, phase: str) -> list["TaskAttempt"]:
    """Work attempts for a ticket and normalized phase, across re-queued task rows."""
    return repair_iterations(_ticket_phase_attempts(ticket_id, phase))


def ticket_phase_observations(ticket_id: int, phase: str) -> list["TaskAttempt"]:
    """Every non-capacity attempt for a ticket and normalized phase, audit rows included."""
    return repair_observations(_ticket_phase_attempts(ticket_id, phase))


def phase_attempts(task: "Task") -> list["TaskAttempt"]:
    """Work attempts for the task's ticket and phase."""
    return ticket_phase_iterations(task.ticket_id, task.phase)


def adjacent_work_attempts(work: list["TaskAttempt"], observations: list["TaskAttempt"]) -> list["TaskAttempt"]:
    """The last two observations only when both are counted work attempts."""
    pair = observations[-2:]
    work_ids = {attempt.pk for attempt in work[-2:]}
    return pair if len(pair) == STALL_REPEAT_THRESHOLD and all(attempt.pk in work_ids for attempt in pair) else []


def next_repair_iteration(attempt: "TaskAttempt") -> int:
    """Stamp a pending attempt against the rows the repair budget will count."""
    if not _is_repair_work(attempt.error):
        return 0
    prior = phase_attempts(attempt.task)
    source_id = cast("int | None", attempt.__dict__["fallback_from_attempt_id"])
    return 1 + sum(row.pk != source_id for row in prior)
