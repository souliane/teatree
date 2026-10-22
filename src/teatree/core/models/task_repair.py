"""Repair-loop model orchestration over the pure ``repair_loop`` policy (#2009).

The model-touching half of the per-phase iteration budget + stall detection:
it reads the recorded ``TaskAttempt`` rows of a ticket-phase, applies the pure
:func:`teatree.core.repair_loop.requeue_verdict`, and on a stall records a
durable user-facing ``DeferredQuestion``. Split out of ``task.py`` (which is at
its module-health LOC cap) — the thin ``Task`` methods delegate here. The
functions take a ``Task`` so they stay free of model-class state.
"""

from typing import TYPE_CHECKING, cast

from django.apps import apps
from django.db.models import Exists, OuterRef

from teatree.core.forge_url import is_synthetic_ticket_url
from teatree.core.modelkit.phases import normalize_phase, phase_spellings
from teatree.core.modelkit.task_failure_taxonomy import (
    LEASE_EXPIRED_PREFIX,
    NON_REPAIR_KINDS,
    classify_failure,
    stall_fingerprints,
)
from teatree.core.models.deferred_question import DeferredQuestion
from teatree.core.models.usage_window_state import PARK_ERROR_PREFIXES
from teatree.core.repair_loop import (
    IterationStalled,
    MaxIterationsExceeded,
    ReofferBudgetExceeded,
    reoffer_verdict,
    requeue_verdict,
)
from teatree.llm.anthropic_limits import recoverable_exhaustion_cause

if TYPE_CHECKING:
    from teatree.core.models.task import Task
    from teatree.core.models.task_attempt import TaskAttempt, TaskAttemptQuerySet


def _attempt_model() -> "type[TaskAttempt]":
    return cast("type[TaskAttempt]", apps.get_model("core", "TaskAttempt"))


def repair_iterations(attempts: "TaskAttemptQuerySet") -> list["TaskAttempt"]:
    """Executed work attempts in a queryset, oldest first."""
    successor = _attempt_model().objects.filter(fallback_from_attempt_id=OuterRef("pk"))
    work = (
        attempts.excluding_parks()
        .exclude(failure_kind__in=NON_REPAIR_KINDS)
        .exclude(error__startswith=LEASE_EXPIRED_PREFIX)
        .filter(~Exists(successor))
        .order_by("pk")
    )
    return [attempt for attempt in work if recoverable_exhaustion_cause(attempt.error) is None]


def ticket_phase_iterations(ticket_id: int, phase: str) -> list["TaskAttempt"]:
    """Work attempts for a ticket and normalized phase, across re-queued task rows."""
    attempts = _attempt_model().objects.filter(
        task__ticket_id=ticket_id,
        task__phase__in=phase_spellings(normalize_phase(phase)),
    )
    return repair_iterations(attempts)


def phase_attempts(task: "Task") -> list["TaskAttempt"]:
    """Work attempts for the task's ticket and phase."""
    return ticket_phase_iterations(task.ticket_id, task.phase)


def next_repair_iteration(attempt: "TaskAttempt") -> int:
    """Stamp a pending attempt against the rows the repair budget will count."""
    if (
        attempt.error.startswith(PARK_ERROR_PREFIXES)
        or classify_failure(attempt.error) in NON_REPAIR_KINDS
        or attempt.error.startswith(LEASE_EXPIRED_PREFIX)
        or recoverable_exhaustion_cause(attempt.error) is not None
    ):
        return 0
    prior = phase_attempts(attempt.task)
    source = attempt.fallback_from_attempt
    source_id = source.pk if source else None
    return 1 + sum(row.pk != source_id for row in prior)


def check_requeue_allowed(task: "Task") -> None:
    """Raise if *task*'s ticket-phase may NOT be re-queued; escalate on a terminal verdict.

    Applies the pure :func:`~teatree.core.repair_loop.requeue_verdict` to the
    recorded attempts of the SAME ``(ticket, normalized-phase)``. Both terminal
    verdicts ALSO record a durable user-facing ``DeferredQuestion`` (§17.1
    invariant 9) before re-raising — so a doomed phase escalates to the user
    instead of freezing silently:

    * :class:`~teatree.core.repair_loop.IterationStalled` — two identical failures;
    * :class:`~teatree.core.repair_loop.MaxIterationsExceeded` — the iteration cap
        (previously FAILed the row with only a ``logger.warning`` — a silent freeze).
    * :class:`~teatree.core.repair_loop.ReofferBudgetExceeded` — the row's own
        re-offer budget, evaluated FIRST because it is the only one of the three
        that still terminates when nothing ever records an attempt.

    A CAUSELESS attempt is dropped from the stall comparison (#4075) but still counted
    toward the cap — see :func:`~teatree.core.modelkit.task_failure_taxonomy.is_causeless`.

    The CAP is skipped for a synthetic cadence-anchor ticket (``architectural_review``,
    ``eval_local``, …) — :func:`~teatree.core.forge_url.is_synthetic_ticket_url`, same
    predicate as the doctor-probe fix (#3492). ``PhaseCadence`` re-fires such a ticket's
    phase forever by design (every ``cadence_hours``/``after_merge_count``, for the
    overlay's whole lifetime), so a LIFETIME attempt count is not a doom signal the way
    it is for a one-shot deliverable ticket — the cap was tripping on ordinary healthy
    recurrence. Stall detection stays live: two consecutive identical failures is still
    a real signal regardless of ticket kind.

    A no-op when under the cap (or cap-exempt) and not stalled.
    """
    phase = normalize_phase(task.phase)
    try:
        reoffer_verdict(
            ticket_id=task.ticket_id,
            phase=phase,
            reclaim_count=task.reclaim_count,
        )
    except ReofferBudgetExceeded:
        _escalate_reoffers(task, phase=phase, reoffers=task.reclaim_count)
        raise
    attempts = phase_attempts(task)
    last_two = stall_fingerprints((a.failure_kind, a.error_fingerprint) for a in attempts[-2:])
    try:
        requeue_verdict(
            ticket_id=task.ticket_id,
            phase=phase,
            iteration_count=len(attempts),
            last_two_fingerprints=last_two,
        )
    except IterationStalled:
        _escalate_stall(task, phase=phase, iterations=len(attempts))
        raise
    except MaxIterationsExceeded:
        if is_synthetic_ticket_url(task.ticket.issue_url):
            return
        _escalate_cap(task, phase=phase, iterations=len(attempts))
        raise


def _escalate_reoffers(task: "Task", *, phase: str, reoffers: int) -> None:
    """Record a durable ``DeferredQuestion`` for an exhausted re-offer budget.

    The signal the re-offer loop had none of: a unit whose outcome nothing records
    circulates behind a frozen completed counter, so neither the stall detector nor
    the iteration cap ever fires and no surface says anything is wrong.
    """
    ticket = task.ticket
    where = ticket.issue_url or f"ticket {ticket.pk}"
    question = (
        f"Re-offer budget on {where} (phase {phase!r}): task {task.pk} has been reclaimed and "
        f"re-offered {reoffers} time(s) without ever terminalizing, so re-offering is paused. "
        f"Its dispatcher is most likely not recording the outcome (`tasks record-attempt`). "
        f"How should it proceed — investigate, rework, or ignore?"
    )
    DeferredQuestion.record(
        question,
        task_session=task.session,
        dedupe_marker=f"reoffer-budget:{ticket.pk}:{phase}",
    )


def _escalate_stall(task: "Task", *, phase: str, iterations: int) -> None:
    """Record a durable ``DeferredQuestion`` for an ``IterationStalled``.

    Reuses the §17.1 invariant 9 away-mode escalation queue — surfaced via the
    statusline, ``t3 teatree questions list``, and the Slack DM drain — rather
    than inventing a new user-facing surface.
    """
    ticket = task.ticket
    where = ticket.issue_url or f"ticket {ticket.pk}"
    question = (
        f"Repair-loop stall on {where} (phase {phase!r}): the last two attempts failed "
        f"identically after {iterations} iteration(s). Re-queueing is paused so it does not "
        f"burn more attempts on the same failure. How should it proceed — investigate, rework, or ignore?"
    )
    # Escalate-once per (ticket, phase): two consecutive stalls on the same
    # ticket-phase collapse to a single queued question rather than one per tick.
    DeferredQuestion.record(
        question,
        task_session=task.session,
        dedupe_marker=f"repair-stall:{ticket.pk}:{phase}",
    )


def _escalate_cap(task: "Task", *, phase: str, iterations: int) -> None:
    """Record a durable, deduped ``DeferredQuestion`` for a ``MaxIterationsExceeded``.

    The iteration-cap verdict previously dropped the row from the re-queue set
    with only a ``logger.warning`` — the ticket froze at that phase with no
    user-facing fingerprint, so the stall detector never fired and no question
    was queued. This surfaces the exhausted budget on the same §17.1 invariant 9
    away-mode escalation queue the stall path uses, deduped per (ticket, phase).
    """
    ticket = task.ticket
    where = ticket.issue_url or f"ticket {ticket.pk}"
    question = (
        f"Repair-loop cap on {where} (phase {phase!r}): the phase hit its iteration cap "
        f"after {iterations} attempt(s) without completing. Re-queueing is paused so it does "
        f"not burn more attempts. How should it proceed — investigate, rework, or ignore?"
    )
    DeferredQuestion.record(
        question,
        task_session=task.session,
        dedupe_marker=f"repair-cap:{ticket.pk}:{phase}",
    )
