"""Apply the per-phase repair budget before returning a task to the queue."""

from typing import TYPE_CHECKING

from teatree.core.forge_url import is_synthetic_ticket_url
from teatree.core.modelkit.phases import normalize_phase
from teatree.core.modelkit.task_failure_taxonomy import stall_fingerprints
from teatree.core.models.task_repair_queries import adjacent_work_attempts, phase_attempts, ticket_phase_observations
from teatree.core.repair_loop import MaxIterationsExceeded, reoffer_verdict, requeue_verdict

if TYPE_CHECKING:
    from teatree.core.models.task import Task


def check_requeue_allowed(task: "Task") -> None:
    """Raise when the re-offer, stall, or iteration budget is exhausted."""
    phase = normalize_phase(task.phase)
    reoffer_verdict(ticket_id=task.ticket_id, phase=phase, reclaim_count=task.reclaim_count)
    attempts = phase_attempts(task)
    adjacent = adjacent_work_attempts(attempts, ticket_phase_observations(task.ticket_id, phase))
    last_two = stall_fingerprints((a.failure_kind, a.error_fingerprint) for a in adjacent)
    try:
        requeue_verdict(
            ticket_id=task.ticket_id,
            phase=phase,
            iteration_count=len(attempts),
            last_two_fingerprints=last_two,
        )
    except MaxIterationsExceeded:
        if not is_synthetic_ticket_url(task.ticket.issue_url):
            raise
