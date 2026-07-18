"""Re-dispatch stuck non-terminal tickets — the drain, hard-bounded (PR-5).

A ticket can freeze in a non-terminal work-state with ZERO open tasks, no open
PR, and no recent activity: its FSM reads ``started``/``planned``/… but nothing is
scheduled to advance it, and the report-only stale scanner never re-dispatches.
This tick sweep schedules the phase task the ticket's state implies
(``started`` → planning, ``planned`` → coding, …), so the frozen ticket resumes.

The re-dispatch is HARD-BOUNDED by the #2009 repair-loop budget: a ticket-phase
at its iteration cap, or stalled on two consecutive identical failures, is NOT
re-dispatched and is escalated LOUDLY via a durable :class:`DeferredQuestion`
(§17.1 invariant 9). Only AUTHOR tickets idle longer than the threshold with no
work in flight are candidates — a ticket with an open task or open PR is already
being worked and is left alone.

Lives in ``teatree.loop`` (orchestration): it composes the ``core`` ticket-
scheduling methods with the ``core`` repair-loop budget over a housekeeping sweep.
"""

import logging
import re
from datetime import datetime

from django.conf import settings
from django.db.models import Max
from django.utils import timezone

from teatree.core.modelkit.phases import normalize_phase, phase_spellings
from teatree.core.models import PullRequest, Task, TaskAttempt, Ticket
from teatree.core.models.deferred_question import DeferredQuestion
from teatree.core.models.errors import InvalidTransitionError
from teatree.core.models.usage_window_state import LIMIT_PARKED_PREFIX
from teatree.core.repair_loop import IterationStalled, MaxIterationsExceeded, requeue_verdict

logger = logging.getLogger(__name__)

DEFAULT_STUCK_IDLE_HOURS = 6

_ESCALATION_MARKER = "[stuck-redispatch-halt ticket={pk}]"
#: Extracts the ticket pk from an escalation marker so an already-escalated ticket is
#: skipped without re-running its per-ticket budget query every tick (bounds the sweep).
_ESCALATION_PK_RE = re.compile(r"\[stuck-redispatch-halt ticket=(\d+)\]")

#: The non-terminal work-states a stuck ticket re-dispatches from, mapped to the
#: phase the state implies. NOT_STARTED / SCOPED await provisioning (excluded);
#: terminal states have nothing left to do.
_STATE_PHASE: dict[str, str] = {
    Ticket.State.STARTED: "planning",
    Ticket.State.PLANNED: "coding",
    Ticket.State.CODED: "testing",
    Ticket.State.TESTED: "reviewing",
    Ticket.State.REVIEWED: "shipping",
}


def redispatch_stuck_tickets() -> int:
    """Schedule the implied phase task for each stuck ticket within budget; escalate the rest.

    Returns the number of tickets re-dispatched (a fresh phase task scheduled).
    """
    now = timezone.now()
    threshold = _idle_threshold_hours()
    already_escalated = _already_escalated_ticket_pks()
    scheduled = 0
    for ticket in _stuck_candidates(now=now, threshold_hours=threshold):
        # An already-escalated ticket is parked — skip it BEFORE its budget query, so
        # the dead-letter set never grows the per-tick work (bounded sweep, #8).
        if ticket.pk in already_escalated:
            continue
        # Per-item fault isolation (#3441): one poison ticket (a budget query that blows
        # up, a scheduling method that raises unexpectedly) must NOT abort the sweep and
        # strand every OTHER stuck ticket. Record it loudly and move on.
        try:
            scheduled += _redispatch_one(ticket)
        except Exception:
            logger.exception("Stuck-redispatch skipped ticket %s after an unexpected error", ticket.pk)
    return scheduled


def _redispatch_one(ticket: Ticket) -> int:
    """Schedule ONE stuck ticket's implied phase within budget, else escalate. Returns 0/1.

    Isolated per ticket so :func:`redispatch_stuck_tickets` can wrap it in a single
    ``try`` and keep sweeping when one row raises.
    """
    phase = _STATE_PHASE[ticket.state]
    halt = _budget_halt_reason(ticket, phase=phase)
    if halt is not None:
        _escalate_once(ticket, reason=halt)
        return 0
    return _redispatch(ticket, phase=phase)


def _already_escalated_ticket_pks() -> set[int]:
    """Ticket pks that already carry a stuck-redispatch escalation (any answered state).

    One query, parsed to a set — an escalated stuck ticket is parked durably, so it is
    never re-escalated when its question is answered/dismissed and never re-budget-
    queried every tick.
    """
    texts = DeferredQuestion.objects.filter(question__contains="[stuck-redispatch-halt ticket=").values_list(
        "question", flat=True
    )
    return {int(m.group(1)) for text in texts if (m := _ESCALATION_PK_RE.search(text))}


def _stuck_candidates(*, now: datetime, threshold_hours: int) -> list[Ticket]:
    """AUTHOR tickets in a work-state with no open task, no open PR, idle past the threshold."""
    tickets = (
        Ticket.objects.filter(state__in=_STATE_PHASE.keys(), role=Ticket.Role.AUTHOR)
        .exclude(tasks__status__in=Task.Status.active())
        .exclude(pull_requests__isnull=False, pull_requests__state__in=_OPEN_PR_STATES)
        .distinct()
    )
    return [t for t in tickets if _is_idle(t, now=now, threshold_hours=threshold_hours)]


#: PR states that count as "open" (a merged PR does not keep a ticket alive).
_OPEN_PR_STATES = frozenset(
    {PullRequest.State.OPEN, PullRequest.State.REVIEW_REQUESTED, PullRequest.State.APPROVED},
)


def _is_idle(ticket: Ticket, *, now: datetime, threshold_hours: int) -> bool:
    """Whether *ticket*'s last recorded activity is older than *threshold_hours*.

    Activity is the newest :class:`TaskAttempt` start or :class:`TicketTransition`
    — the same signal the stale scanner reads. A ticket with NO activity record at
    all cannot be aged, so it is conservatively treated as NOT idle (the ``start``
    transition always writes a transition row, so a genuinely stuck ticket always
    has one).
    """
    last = _last_activity(ticket)
    if last is None:
        return False
    return (now - last).total_seconds() >= threshold_hours * 3600


def _last_activity(ticket: Ticket) -> datetime | None:
    last_attempt = ticket.tasks.aggregate(ts=Max("attempts__started_at"))["ts"]  # ty: ignore[unresolved-attribute]  # Django reverse FK
    if last_attempt is not None:
        return last_attempt
    return ticket.transitions.aggregate(ts=Max("created_at"))["ts"]  # ty: ignore[unresolved-attribute]  # Django reverse FK


def _redispatch(ticket: Ticket, *, phase: str) -> int:
    """Schedule the phase task *ticket*'s state implies; escalate on a scheduling refusal. Returns 0/1."""
    try:
        _schedule_for_state(ticket)
    except InvalidTransitionError as exc:
        _escalate_once(ticket, reason=f"could not schedule {phase!r}: {exc}")
        return 0
    return 1


def _schedule_for_state(ticket: Ticket) -> Task:
    state = ticket.state
    if state == Ticket.State.STARTED:
        return ticket.schedule_planning()
    if state == Ticket.State.PLANNED:
        return ticket.schedule_coding()
    if state == Ticket.State.CODED:
        return ticket.schedule_testing()
    if state == Ticket.State.TESTED:
        return ticket.schedule_review()
    return ticket.schedule_shipping()


def _budget_halt_reason(ticket: Ticket, *, phase: str) -> str | None:
    """Return the loud halt reason if *ticket*'s phase is out of repair budget, else ``None``."""
    attempts = _phase_attempts(ticket, phase=phase)
    last_two = [a.error_fingerprint for a in attempts[-2:] if a.error_fingerprint]
    try:
        requeue_verdict(
            ticket_id=ticket.pk,
            phase=normalize_phase(phase),
            iteration_count=len(attempts),
            last_two_fingerprints=last_two,
        )
    except (MaxIterationsExceeded, IterationStalled) as exc:
        return str(exc)
    return None


def _phase_attempts(ticket: Ticket, *, phase: str) -> list[TaskAttempt]:
    """WORK attempts of *ticket*'s ``(ticket, normalized-phase)``, oldest first (limit-parks excluded)."""
    return list(
        TaskAttempt.objects.filter(
            task__ticket_id=ticket.pk,
            task__phase__in=phase_spellings(normalize_phase(phase)),
        )
        .exclude(error__startswith=LIMIT_PARKED_PREFIX)
        .order_by("pk"),
    )


def _escalate_once(ticket: Ticket, *, reason: str) -> None:
    """Record a durable escalation for a budget-halted stuck ticket, once per ticket.

    Idempotent: a per-ticket marker deduped across ALL questions (answered or not) so a
    halted stuck ticket escalates exactly once and answering/dismissing the question
    never resurrects a fresh one. Reuses the §17.1 invariant 9 surface (statusline /
    ``t3 teatree questions list`` / Slack DM).
    """
    marker = _ESCALATION_MARKER.format(pk=ticket.pk)
    already = DeferredQuestion.objects.filter(question__contains=marker).exists()
    if already:
        return
    where = ticket.issue_url or f"ticket {ticket.pk}"
    question = (
        f"{marker} Stuck ticket {where} (state {ticket.state!r}) has no work in flight but "
        f"re-dispatch is halted: {reason} Auto-scheduling is stopped so it does not re-run a "
        "doomed phase forever. How should it proceed — investigate, rework, or ignore?"
    )
    DeferredQuestion.record(question, session_id="")


def _idle_threshold_hours() -> int:
    """Configured stuck-idle threshold (``STUCK_TICKET_IDLE_HOURS``, floor 1)."""
    raw = getattr(settings, "STUCK_TICKET_IDLE_HOURS", DEFAULT_STUCK_IDLE_HOURS)
    try:
        value = int(raw)
    except (TypeError, ValueError):
        return DEFAULT_STUCK_IDLE_HOURS
    return max(1, value)
