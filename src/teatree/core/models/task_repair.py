"""Repair-loop model orchestration over the pure ``repair_loop`` policy (#2009).

The model-touching half of the per-phase iteration budget + stall detection:
it reads the recorded ``TaskAttempt`` rows of a ticket-phase, applies the pure
:func:`teatree.core.repair_loop.requeue_verdict`, and on a stall records a
durable user-facing ``DeferredQuestion``. Split out of ``task.py`` (which is at
its module-health LOC cap) — the thin ``Task`` methods delegate here. The
functions take a ``Task`` so they stay free of model-class state.
"""

from teatree.core.modelkit.phases import normalize_phase, phase_spellings
from teatree.core.models.deferred_question import DeferredQuestion
from teatree.core.models.task import Task
from teatree.core.models.task_attempt import TaskAttempt
from teatree.core.repair_loop import IterationStalled, requeue_verdict


def phase_attempts(task: Task) -> list[TaskAttempt]:
    """Every attempt of *task*'s ``(ticket, normalized-phase)``, oldest first.

    Spans the re-queued ``Task`` rows of the same ticket-phase — a re-queue
    creates a NEW ``Task`` row, so the iteration sequence is keyed on the ticket
    + canonical phase, not a single ``Task``.
    """
    return list(
        TaskAttempt.objects.filter(
            task__ticket_id=task.ticket_id,  # ty: ignore[unresolved-attribute]
            task__phase__in=phase_spellings(normalize_phase(task.phase)),
        ).order_by("pk"),
    )


def check_requeue_allowed(task: Task) -> None:
    """Raise if *task*'s ticket-phase may NOT be re-queued; escalate on a stall.

    Applies the pure :func:`~teatree.core.repair_loop.requeue_verdict` to the
    recorded attempts of the SAME ``(ticket, normalized-phase)``; on
    :class:`~teatree.core.repair_loop.IterationStalled` ALSO records a durable
    user-facing ``DeferredQuestion`` (§17.1 invariant 9) before re-raising — so
    the loop escalates to the user instead of re-running the identical failure.
    A no-op when under the cap and not stalled.
    """
    attempts = phase_attempts(task)
    phase = normalize_phase(task.phase)
    last_two = [a.error_fingerprint for a in attempts[-2:] if a.error_fingerprint]
    try:
        requeue_verdict(
            ticket_id=task.ticket_id,  # ty: ignore[unresolved-attribute]
            phase=phase,
            iteration_count=len(attempts),
            last_two_fingerprints=last_two,
        )
    except IterationStalled:
        _escalate_stall(task, phase=phase, iterations=len(attempts))
        raise


def _escalate_stall(task: Task, *, phase: str, iterations: int) -> None:
    """Record a durable ``DeferredQuestion`` for an ``IterationStalled``.

    Reuses the §17.1 invariant 9 away-mode escalation queue — surfaced via the
    statusline, ``t3 teatree questions list``, and the Slack DM drain — rather
    than inventing a new user-facing surface.
    """
    ticket = task.ticket
    where = ticket.issue_url or f"ticket {ticket.pk}"
    session_id: int | None = task.session_id  # ty: ignore[unresolved-attribute]
    question = (
        f"Repair-loop stall on {where} (phase {phase!r}): the last two attempts failed "
        f"identically after {iterations} iteration(s). Re-queueing is paused so it does not "
        f"burn more attempts on the same failure. How should it proceed — investigate, rework, or ignore?"
    )
    DeferredQuestion.record(question, session_id=str(session_id or ""))
