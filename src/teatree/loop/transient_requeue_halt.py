"""Durable terminal failure handling for the transient requeue sweep."""

import logging
from functools import partial

from django.db import transaction

from teatree.core.forge_url import is_synthetic_ticket_url
from teatree.core.modelkit.phases import ARCHITECTURAL_REVIEW_PHASE, normalize_phase
from teatree.core.modelkit.task_failure_taxonomy import HALT_STAMP, FailureKind, classify_failure
from teatree.core.models import Task, TaskAttempt
from teatree.core.owner_dm.architectural_review_notice import announce_failed
from teatree.core.repair_loop import terminal_reason_fingerprint

logger = logging.getLogger(__name__)


def _is_review_anchor(task: Task) -> bool:
    return normalize_phase(task.phase) == ARCHITECTURAL_REVIEW_PHASE and is_synthetic_ticket_url(task.ticket.issue_url)


def _latest_attempt(task: Task) -> TaskAttempt | None:
    attempts = sorted(task.attempts.all(), key=lambda attempt: attempt.pk)
    return attempts[-1] if attempts else None


def _latest_error(task: Task) -> str:
    attempt = _latest_attempt(task)
    return attempt.error if attempt is not None else ""


def _stamp_halt(task: Task) -> None:
    if HALT_STAMP in task.execution_reason:
        return
    reason = f"{task.execution_reason}\n{HALT_STAMP}".strip() if task.execution_reason else HALT_STAMP
    Task.objects.filter(pk=task.pk).update(execution_reason=reason)


def escalation_marker(task: Task) -> str:
    """Reconstruct a legacy halt-question key for the old-question drain."""
    fingerprint = terminal_reason_fingerprint(_latest_error(task))
    phase = normalize_phase(task.phase)
    return f"repair-halt:{phase}:{fingerprint}"[:64]


def _escalate_once(task: Task, *, reason: str) -> None:
    """Stamp a failed task for health reporting and announce terminal reviews."""
    logger.warning("Task %s stopped automatic retry: %s", task.pk, reason)
    with transaction.atomic():
        _stamp_halt(task)
        task.ticket.pop_task_thread(int(task.pk))
        if _is_review_anchor(task):
            error = _latest_error(task)
            failure = f"{FailureKind(classify_failure(error)).label}: {reason}" if error else reason
            transaction.on_commit(partial(announce_failed, task, failure), robust=True)
