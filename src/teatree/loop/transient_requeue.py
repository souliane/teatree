"""Bounded auto-requeue of transient-FAILED tasks — the retry, hard-bounded.

``Task.fail()`` is terminal: a task that RETURNS a failure envelope (an outage,
a provisioning-step failure, an incomplete run, a coder yield that landed no
commit) lands FAILED and stays there forever — the crashed-session reclaim
(``reclaim_orphaned_claims``) only rescues expired-lease CLAIMED rows, never a
returned failure. This tick sweep reopens such a row (FAILED → PENDING) so the
next dispatch resumes it.

The retry is HARD-BOUNDED by the #2009 repair-loop budget so it can never retry
endlessly (it would always fail): a ticket-phase at its iteration cap, or stalled
on two consecutive identical failures, is NOT reopened and is escalated LOUDLY
via a durable :class:`DeferredQuestion` (§17.1 invariant 9) — never silently,
never forever. Only a TRANSIENT last-attempt failure is a candidate; a
DETERMINISTIC failure (a test failure, an assertion, a schema/evidence refusal)
stays terminal FAILED.

Lives in ``teatree.loop`` (orchestration): it needs both the transient classifier
(``teatree.agents``) and the ``Task`` model (``teatree.core.models``), which sit
in the same ``domain`` layer and so cannot import each other — only an
orchestration-layer module may compose both.
"""

import logging

from teatree.agents.outage_classifier import is_transient_failure
from teatree.core.modelkit.phases import normalize_phase
from teatree.core.models import Task, Ticket
from teatree.core.models.deferred_question import DeferredQuestion
from teatree.core.models.task_repair import phase_attempts
from teatree.core.repair_loop import IterationStalled, MaxIterationsExceeded, requeue_verdict

logger = logging.getLogger(__name__)

_ESCALATION_MARKER = "[repair-halt task={pk}]"


def requeue_transient_failed() -> int:
    """Reopen transient-FAILED tasks within the repair budget; escalate the rest. Returns the reopen count."""
    reopened = 0
    for task in _transient_failed_candidates():
        halt = _budget_halt_reason(task)
        if halt is None:
            reopened += _reopen(task)
        else:
            _escalate_once(task, reason=halt)
    return reopened


def _transient_failed_candidates() -> list[Task]:
    """FAILED tasks on a non-terminal ticket whose LATEST attempt is a transient failure."""
    candidates: list[Task] = []
    failed = (
        Task.objects.filter(status=Task.Status.FAILED)
        .exclude(ticket__state__in=Ticket._TERMINAL_STATES)  # noqa: SLF001 — the model's SSOT terminal set
        .select_related("ticket")
    )
    for task in failed:
        last = task.attempts.order_by("-pk").first()  # ty: ignore[unresolved-attribute]
        if last is not None and is_transient_failure(last.error):
            candidates.append(task)
    return candidates


def _budget_halt_reason(task: Task) -> str | None:
    """Return the loud halt reason if *task*'s phase is out of budget, else ``None`` (may requeue).

    Uses the pure :func:`~teatree.core.repair_loop.requeue_verdict` over the SAME
    recorded attempts the reclaim path budgets on, WITHOUT the escalation side
    effect of ``Task.check_requeue_allowed`` — this sweep escalates both the cap
    AND the stall itself (idempotently), which that helper does only for the
    stall.
    """
    attempts = phase_attempts(task)
    last_two = [a.error_fingerprint for a in attempts[-2:] if a.error_fingerprint]
    try:
        requeue_verdict(
            ticket_id=task.ticket_id,  # ty: ignore[unresolved-attribute]
            phase=normalize_phase(task.phase),
            iteration_count=len(attempts),
            last_two_fingerprints=last_two,
        )
    except (MaxIterationsExceeded, IterationStalled) as exc:
        return str(exc)
    return None


def _reopen(task: Task) -> int:
    """CAS FAILED → PENDING; returns 1 on the winning transition, 0 if already moved.

    A single conditional ``UPDATE ... WHERE status=FAILED`` (the same
    backend-agnostic compare-and-swap ``reclaim_orphaned_claims`` uses) so a
    concurrent tick that already reopened the row updates 0 rows and does not
    double-dispatch.
    """
    return Task.objects.filter(pk=task.pk, status=Task.Status.FAILED).update(
        status=Task.Status.PENDING,
        claimed_at=None,
        claimed_by="",
        claimed_by_session="",
        lease_expires_at=None,
        heartbeat_at=None,
    )


def _escalate_once(task: Task, *, reason: str) -> None:
    """Record a durable escalation for a budget-halted task, once per task.

    Idempotent: a per-task marker in the question text dedups so a halted FAILED
    row re-scanned every tick escalates exactly once rather than spamming the
    away-mode question queue. Reuses the §17.1 invariant 9 surface (statusline /
    ``t3 teatree questions list`` / the Slack DM drain).
    """
    marker = _ESCALATION_MARKER.format(pk=task.pk)
    already = DeferredQuestion.objects.filter(
        answered_at__isnull=True,
        dismissed_at__isnull=True,
        question__contains=marker,
    ).exists()
    if already:
        return
    where = task.ticket.issue_url or f"ticket {task.ticket_id}"
    question = (
        f"{marker} Auto-retry halted on {where} (phase {normalize_phase(task.phase)!r}): {reason} "
        "Re-queueing is stopped so it does not retry a doomed failure forever. "
        "How should it proceed — investigate, rework, or ignore?"
    )
    DeferredQuestion.record(question, session_id=str(task.session_id or ""))  # ty: ignore[unresolved-attribute]
