"""Re-dispatch stuck non-terminal tickets — the drain, hard-bounded (PR-5, #3958).

Two populations freeze the factory, and one sweep drains both.

A **frozen** ticket sits in a non-terminal state with ZERO open tasks, no open PR,
and no recent activity: its FSM reads ``started``/``planned``/… but nothing is
scheduled to advance it, and the report-only stale scanner never re-dispatches.

A **failing** ticket is not idle at all: its latest attempt for the implied phase
FAILED and nothing is in flight, so it churns rather than stops and an idle
threshold can never reach it. Both roles are covered — the failing population is
dominated by the ``reviewing`` phase, which lives on REVIEWER tickets, and those sit
at ``not_started`` until ``review_posted``, so their implied phase comes from their
own most recent task rather than from a state map. A failure whose phase output
DEMONSTRABLY LANDED is excluded: it is a dead artifact, and re-running it is the
already-done redispatch flood the ``transient_requeue`` sweep retires it to avoid.

The re-dispatch is HARD-BOUNDED by the #2009 repair-loop budget, on ONE path for
both classes: a ticket-phase at its iteration cap, stalled on two consecutive
identical failures, or stalled on two consecutive failures of the same NAMED
DETERMINISTIC :class:`FailureKind`, is NOT re-dispatched and is escalated LOUDLY via
a durable :class:`DeferredQuestion` (§17.1 invariant 9). The named-cause stall is
what stops a repair storm: re-dispatching into a deterministic defect reproduces it,
while an environmental fault is the environment's and stays retryable up to the cap.
A ticket with an open task is already being worked and is left alone.

Lives in ``teatree.loop`` (orchestration): it composes the ``core`` ticket-
scheduling methods with the ``core`` repair-loop budget over a housekeeping sweep.
"""

import logging
import re
from dataclasses import dataclass
from datetime import datetime

from django.conf import settings
from django.db.models import Max, Q
from django.utils import timezone

from teatree.core.modelkit.phases import normalize_phase, phase_spellings
from teatree.core.modelkit.task_failure_taxonomy import FailureKind, is_causeless, is_environmental, stall_fingerprints
from teatree.core.models import PullRequest, Task, TaskAttempt, Ticket
from teatree.core.models.deferred_question import DeferredQuestion
from teatree.core.models.errors import InvalidTransitionError
from teatree.core.models.phase_landing import phase_landing_evidence
from teatree.core.models.ticket_external_review import schedule_external_review
from teatree.core.models.usage_window_state import LIMIT_PARKED_PREFIX
from teatree.core.repair_loop import IterationStalled, MaxIterationsExceeded, requeue_verdict
from teatree.llm.anthropic_limits import recoverable_exhaustion_cause
from teatree.loop.persistence_phase_task import create_phase_task

logger = logging.getLogger(__name__)

DEFAULT_STUCK_IDLE_HOURS = 6

_ESCALATION_MARKER = "[stuck-redispatch-halt ticket={pk}]"
#: Extracts the ticket pk from an escalation marker so an already-escalated ticket is
#: skipped without re-running its per-ticket budget query every tick (bounds the sweep).
_ESCALATION_PK_RE = re.compile(r"\[stuck-redispatch-halt ticket=(\d+)\]")

#: The non-terminal work-states an AUTHOR ticket re-dispatches from, mapped to the
#: phase the state implies. NOT_STARTED / SCOPED await provisioning (excluded);
#: terminal states have nothing left to do. A reviewer ticket has no equivalent map
#: — see :func:`_implied_phase`.
_STATE_PHASE: dict[str, str] = {
    Ticket.State.STARTED: "planning",
    Ticket.State.PLANNED: "coding",
    Ticket.State.CODED: "testing",
    Ticket.State.TESTED: "reviewing",
    Ticket.State.REVIEWED: "shipping",
}

#: Attempt outcomes that mean the phase's last run did not succeed (#16's explicit
#: discriminator, so an envelope refusal recorded with ``exit_code=0`` still counts).
_FAILED_OUTCOMES = frozenset({TaskAttempt.Outcome.REFUSAL, TaskAttempt.Outcome.CRASH})


@dataclass(frozen=True)
class _Candidate:
    """One ticket-phase the sweep may re-dispatch — the unit both classes reduce to."""

    ticket: Ticket
    phase: str


def redispatch_stuck_tickets() -> int:
    """Schedule the implied phase task for each stuck ticket within budget; escalate the rest.

    Returns the number of tickets re-dispatched (a fresh phase task scheduled).
    """
    now = timezone.now()
    threshold = _idle_threshold_hours()
    already_escalated = _already_escalated_ticket_pks()
    scheduled = 0
    for candidate in _stuck_candidates(now=now, threshold_hours=threshold):
        # An already-escalated ticket is parked — skip it BEFORE its budget query, so
        # the dead-letter set never grows the per-tick work (bounded sweep, #8).
        if candidate.ticket.pk in already_escalated:
            continue
        # Per-item fault isolation (#3441): one poison ticket (a budget query that blows
        # up, a scheduling method that raises unexpectedly) must NOT abort the sweep and
        # strand every OTHER stuck ticket. Record it loudly and move on.
        try:
            scheduled += _redispatch_one(candidate)
        except Exception:
            logger.exception("Stuck-redispatch skipped ticket %s after an unexpected error", candidate.ticket.pk)
    return scheduled


def _redispatch_one(candidate: _Candidate) -> int:
    """Schedule ONE candidate's phase within budget, else escalate. Returns 0/1.

    Isolated per candidate so :func:`redispatch_stuck_tickets` can wrap it in a single
    ``try`` and keep sweeping when one row raises. The single path both classes take,
    so no re-dispatch can reach the scheduler without passing the budget.
    """
    halt = _budget_halt_reason(candidate.ticket, phase=candidate.phase)
    if halt is not None:
        _escalate_once(candidate.ticket, reason=halt)
        return 0
    return _redispatch(candidate)


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


def _stuck_candidates(*, now: datetime, threshold_hours: int) -> list[_Candidate]:
    """Every ticket-phase with no work in flight that is either frozen or failing.

    One queryset, two admission predicates OR-ed: a ticket idle past *threshold_hours*
    (frozen) or one whose latest attempt for the implied phase failed (failing).
    """
    candidates = []
    for ticket in _live_tickets_with_nothing_in_flight():
        phase = _implied_phase(ticket)
        if phase is None:
            continue
        if _phase_is_failing(ticket, phase=phase) or _is_idle(ticket, now=now, threshold_hours=threshold_hours):
            candidates.append(_Candidate(ticket=ticket, phase=phase))
    return candidates


def _live_tickets_with_nothing_in_flight() -> list[Ticket]:
    """Non-terminal tickets of either role carrying no active task (and, for an author, no open PR).

    The open-PR exclusion is AUTHOR-only on purpose: an author's open PR means the work
    is in flight, whereas a reviewer ticket's PR IS its subject — every reviewer ticket
    has one open by definition, so excluding on it would silently re-narrow the sweep
    back to author-only.
    """
    author = Q(role=Ticket.Role.AUTHOR, state__in=tuple(_STATE_PHASE))
    reviewer = Q(role=Ticket.Role.REVIEWER) & ~Q(state__in=tuple(_REVIEWER_DONE_STATES))
    open_pr = Q(role=Ticket.Role.AUTHOR, pull_requests__state__in=_OPEN_PR_STATES)
    return list(
        Ticket.objects.filter(author | reviewer)
        .exclude(tasks__status__in=Task.Status.active())
        .exclude(open_pr)
        .distinct()
    )


def _implied_phase(ticket: Ticket) -> str | None:
    """The phase this ticket's next re-dispatch should schedule, or ``None`` to skip it.

    An author ticket's phase follows its FSM state. A reviewer ticket has no such map —
    it is minted at NOT_STARTED and stays there until REVIEW_POSTED — so its phase is
    the one its own most recent task ran, which also preserves the codex review variants
    (``codex_reviewing`` / ``codex_adversarial_reviewing``) a plain ``reviewing`` would
    collapse. A reviewer ticket that never ran a task has no phase to imply and no
    failure to repair, so the sweep leaves it alone rather than inventing work.
    """
    if ticket.role != Ticket.Role.REVIEWER:
        return _STATE_PHASE.get(ticket.state)
    tasks = ticket.tasks.order_by("-pk")  # ty: ignore[unresolved-attribute]  # Django reverse FK
    return tasks.values_list("phase", flat=True).first() or None


def _phase_is_failing(ticket: Ticket, *, phase: str) -> bool:
    """Whether the latest recorded WORK attempt of *ticket*'s *phase* failed and left nothing behind.

    A failed attempt whose phase output DEMONSTRABLY LANDED is not a failing phase — it
    is the dead artifact of an interrupted run the ticket already advanced past, and the
    same ``transient_requeue`` sweep that runs before this one retires it COMPLETED for
    exactly that reason. Re-dispatching it is the already-done redispatch flood
    (3366/3336/3352), and the failing class is what would reach it: unlike a frozen
    ticket it never has to wait out an idle threshold first.

    The phase-artifact half of that evidence — an attached PR for shipping, a recorded
    verdict at the reviewed head for a review phase — is trusted only for a LEASE_LOST
    failure (#3982, #4100): either can exist independently of THIS attempt, so trusting
    one for a genuinely deterministic failure would hide a real, reproducible defect from
    this same sweep.
    """
    attempts = _phase_attempts(ticket, phase=phase)
    if not attempts or attempts[-1].outcome not in _FAILED_OUTCOMES:
        return False
    latest = attempts[-1]
    return not phase_landing_evidence(latest.task, trust_phase_artifact=latest.failure_kind == FailureKind.LEASE_LOST)


#: PR states that count as "open" (a merged PR does not keep a ticket alive).
_OPEN_PR_STATES = frozenset(
    {PullRequest.State.OPEN, PullRequest.State.REVIEW_REQUESTED, PullRequest.State.APPROVED},
)

#: States a REVIEWER ticket has nothing left to do in. ``marker_release_states()``
#: carries the reviewer terminal (REVIEW_POSTED); RETROSPECTED is added for the same
#: reason the failed-task doctor probe adds it — a retrospected ticket is finished.
_REVIEWER_DONE_STATES = Ticket.marker_release_states() | {Ticket.State.RETROSPECTED}


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


def _redispatch(candidate: _Candidate) -> int:
    """Schedule the candidate's phase task; escalate on a scheduling refusal. Returns 0/1."""
    ticket = candidate.ticket
    try:
        _schedule_for_candidate(candidate)
    except InvalidTransitionError as exc:
        _escalate_once(ticket, reason=f"could not schedule {candidate.phase!r}: {exc}")
        return 0
    return 1


def _schedule_for_candidate(candidate: _Candidate) -> Task:
    """Mint the candidate's phase task through the seam that owns that phase.

    The author FSM mints and :func:`create_phase_task` are CAS-guarded and return an
    in-flight sibling rather than racing one. :func:`schedule_external_review` is not —
    it has always leaned on its caller's open-task pre-check, which here is the
    ``no active task`` admission predicate every candidate already passed.
    """
    ticket, phase = candidate.ticket, candidate.phase
    if ticket.role != Ticket.Role.REVIEWER:
        return _schedule_for_state(ticket)
    if normalize_phase(phase) == "reviewing":
        return schedule_external_review(ticket)
    return create_phase_task(
        ticket,
        phase=phase,
        agent_id=phase,
        reason=f"Auto-repair re-dispatch — {phase} on {ticket.issue_url or ticket.pk}",
    )


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
    last_two = stall_fingerprints((a.failure_kind, a.error_fingerprint) for a in attempts[-2:])
    try:
        requeue_verdict(
            ticket_id=ticket.pk,
            phase=normalize_phase(phase),
            iteration_count=len(attempts),
            last_two_fingerprints=last_two,
            last_two_deterministic_kinds=_deterministic_kinds(attempts),
        )
    except (MaxIterationsExceeded, IterationStalled) as exc:
        return str(exc)
    return None


#: Kinds that are the ABSENCE of a name rather than a cause, so two of them are not
#: evidence of one repeating defect — two unrelated failures both land here. They keep
#: the text-based fingerprint stall, which compares what actually differs between them.
_UNNAMED_KINDS = frozenset({FailureKind.UNCLASSIFIED, FailureKind.UNRECORDED})


def _deterministic_kinds(attempts: list[TaskAttempt]) -> list[str]:
    """The last two attempts' failure kinds, keeping only the NAMED deterministic ones (#3957).

    An environmental kind is dropped rather than compared: a lost lease or an API outage
    is the environment's fault, so repeating it says nothing about the work and must stay
    retryable up to the iteration cap. A CAUSELESS kind is dropped for the sibling reason
    (#4075) — it reports the absence of a cause, so two of them are one silence repeated,
    not one defect. Dropping (rather than substituting a placeholder) also means one such
    failure between two identical deterministic ones breaks the run — only two CONSECUTIVE
    named deterministic failures halt.
    """
    return [
        a.failure_kind
        for a in attempts[-2:]
        if a.failure_kind
        and a.failure_kind not in _UNNAMED_KINDS
        and not is_causeless(a.failure_kind)
        and not is_environmental(a.failure_kind)
    ]


def _phase_attempts(ticket: Ticket, *, phase: str) -> list[TaskAttempt]:
    """WORK attempts of *ticket*'s ``(ticket, normalized-phase)``, oldest first.

    Both a usage-window limit-PARK (``LIMIT_PARKED_PREFIX``) and a window-recoverable
    exhaustion FAILURE (session/weekly/rate-limit — see
    :func:`~teatree.llm.anthropic_limits.recoverable_exhaustion_cause`) are excluded: a
    capacity dip never executed a work iteration, so it must not burn the repair budget
    nor trip the identical-failure stall — otherwise two limit hits spuriously halt the
    re-dispatch and page a human. API-credit exhaustion (no timed reset) still counts.
    """
    attempts = (
        TaskAttempt.objects.filter(
            task__ticket_id=ticket.pk,
            task__phase__in=phase_spellings(normalize_phase(phase)),
        )
        .exclude(error__startswith=LIMIT_PARKED_PREFIX)
        .order_by("pk")
    )
    return [attempt for attempt in attempts if recoverable_exhaustion_cause(attempt.error) is None]


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
