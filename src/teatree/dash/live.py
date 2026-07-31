"""The live-work read model: what the factory is executing right now (#3856, #3886).

The board renders ticket FSM state, which is a LAGGING view — it answers "where did
work get to", never "is anything running at this moment". That left the single question
an operator asks most often about an autonomous system with no surface at all: the only
way to learn what was in flight was to read the database by hand.

Four panels, each a rendering of state that is ALREADY recorded — nothing here is
derived at render time, because a derivation reports today's answer about yesterday's
dispatch:

* **Running** — ``TaskAttempt`` rows with no ``ended_at``. That is the definition of
    an attempt in flight, and it carries the dispatch provenance the operator needs to
    judge it: phase, execution target, lane, model, elapsed, and the resolved bundle.
* **Queue** — ``Task.status``: how many are PENDING, how many CLAIMED, and which are
    next. A queue that is not draining is visible here before it is anywhere else.
* **Loops** — every registered mini-loop, its cadence anchors, and *why the live tick
    would refuse it* when it would. That reason is the tick's own
    (:func:`teatree.loops.loop_table.loop_block_reasons`), never a second vocabulary:
    a refused loop and a loop that swept and found nothing both produce no work, and
    only the reason tells them apart.
* **Outcomes** — a short tail of attempts that have ENDED, with parks marked, so a
    park storm is visible while it happens rather than by counting rows days later.

**No free text an agent produced reaches this page.** A row carries dispatch facts
only — never an attempt's ``error`` body — which is the same rule the session index
follows and the reason a configured secret cannot appear in the response bytes. The
transcript click-through remains the one surface that shows agent output, and it
routes every line through the shared leak-gate redactor first.
"""

import datetime as dt
from dataclasses import dataclass

from django.db.models import Count
from django.utils import timezone

from teatree.core.models.loop import Loop
from teatree.core.models.task import Task
from teatree.core.models.task_attempt import TaskAttempt
from teatree.core.models.usage_window_state import LIMIT_PARKED_PREFIX
from teatree.core.selectors._helpers import _humanize_duration
from teatree.dash.skills import skill_bundle
from teatree.loops.loop_table import loop_block_reasons

#: Running attempts listed. A healthy box runs a handful; a cap keeps a runaway
#: fan-out from rendering a page instead of reporting one.
RUNNING_ROWS = 40

#: Queued tasks previewed under the counts — "what is next", not the whole backlog.
QUEUE_PREVIEW_ROWS = 15

#: Terminal outcomes in the tail. Short on purpose: this panel exists to make a park
#: storm or a refusal run visible AS IT HAPPENS, which a long history obscures.
LIVE_OUTCOME_ROWS = 15


@dataclass(frozen=True, slots=True)
class RunningRow:
    """One attempt that has started and not ended — the definition of 'running'."""

    attempt_id: int
    ticket_id: int | None
    ticket_number: str
    short_description: str
    phase: str
    execution_target: str
    lane: str
    model: str
    agent_session_id: str
    started_at: dt.datetime
    elapsed: str
    skills: tuple[str, ...]
    skills_fault: bool


@dataclass(frozen=True, slots=True)
class QueuedRow:
    """One task waiting to be picked up, or already claimed and not yet attempted."""

    task_id: int
    ticket_id: int | None
    ticket_number: str
    short_description: str
    phase: str
    status: str
    claimed_by: str
    execution_target: str


@dataclass(frozen=True, slots=True)
class LoopRow:
    """One registered mini-loop's liveness — and why the tick would refuse it."""

    name: str
    last_run_at: dt.datetime | None
    next_run_at: dt.datetime | None
    cadence_label: str
    blocked_reason: str

    @property
    def dispatching(self) -> bool:
        """Whether the live tick would fan this loop out right now."""
        return not self.blocked_reason


@dataclass(frozen=True, slots=True)
class OutcomeRow:
    """One attempt that has ENDED — a completion, a refusal, a crash or a park."""

    attempt_id: int
    ticket_number: str
    phase: str
    outcome: str
    exit_code: int | None
    ended_at: dt.datetime | None
    duration: str
    parked: bool
    park_repeats: int


@dataclass(frozen=True, slots=True)
class LiveView:
    """The whole page in one value, stamped so a frozen poll is obvious."""

    running: tuple[RunningRow, ...]
    pending_count: int
    claimed_count: int
    queued: tuple[QueuedRow, ...]
    loops: tuple[LoopRow, ...]
    outcomes: tuple[OutcomeRow, ...]
    generated_at: dt.datetime

    @property
    def anything_running(self) -> bool:
        return bool(self.running)


def build_live_view() -> LiveView:
    """Assemble every panel. One bounded query per panel — this page is polled."""
    now = timezone.now()
    counts = _queue_counts()
    return LiveView(
        running=_running(now),
        pending_count=counts.get(Task.Status.PENDING, 0),
        claimed_count=counts.get(Task.Status.CLAIMED, 0),
        queued=_queued(),
        loops=_loops(now),
        outcomes=_outcomes(),
        generated_at=now,
    )


def _running(now: dt.datetime) -> tuple[RunningRow, ...]:
    attempts = (
        TaskAttempt.objects.filter(ended_at__isnull=True)
        .select_related("task", "task__ticket")
        .order_by("-started_at", "-pk")[:RUNNING_ROWS]
    )
    return tuple(_running_row(attempt, now) for attempt in attempts)


def _running_row(attempt: TaskAttempt, now: dt.datetime) -> RunningRow:
    ticket = attempt.task.ticket
    skills, fault = skill_bundle(attempt)
    return RunningRow(
        attempt_id=attempt.pk,
        ticket_id=ticket.pk if ticket else None,
        ticket_number=ticket.ticket_number if ticket else "",
        short_description=ticket.short_description if ticket else "",
        phase=attempt.task.phase,
        execution_target=str(attempt.execution_target),
        lane=str(attempt.lane),
        model=attempt.model,
        agent_session_id=attempt.agent_session_id,
        started_at=attempt.started_at,
        elapsed=_humanize_duration((now - attempt.started_at).total_seconds()),
        skills=skills,
        skills_fault=fault,
    )


def _queue_counts() -> dict[str, int]:
    """PENDING / CLAIMED task counts in ONE grouped read.

    Grouped rather than two ``count()`` calls, because this page is polled and the
    two numbers must describe the same instant — a queue read as 12 pending and then
    (a query later) 0 claimed reports a state that never existed.
    """
    rows = (
        Task.objects.filter(status__in=(Task.Status.PENDING, Task.Status.CLAIMED))
        .values("status")
        .annotate(total=Count("pk"))
    )
    return {str(row["status"]): int(row["total"]) for row in rows}


def _queued() -> tuple[QueuedRow, ...]:
    tasks = (
        Task.objects.filter(status__in=(Task.Status.PENDING, Task.Status.CLAIMED))
        .select_related("ticket")
        .order_by("-pk")[:QUEUE_PREVIEW_ROWS]
    )
    return tuple(
        QueuedRow(
            task_id=task.pk,
            ticket_id=task.ticket_id,
            ticket_number=task.ticket.ticket_number if task.ticket_id else "",
            short_description=task.ticket.short_description if task.ticket_id else "",
            phase=task.phase,
            status=str(task.status),
            claimed_by=task.claimed_by,
            execution_target=str(task.execution_target),
        )
        for task in tasks
    )


def _loops(now: dt.datetime) -> tuple[LoopRow, ...]:
    """Every REGISTERED mini-loop, with its anchors and the tick's refusal reason.

    Registry-keyed rather than row-keyed: a ``Loop`` row naming no registered
    mini-loop is not something the live tick dispatches, and a registered loop with
    no row is a real misconfiguration the reason itself already states.
    """
    rows = {row.name: row for row in Loop.objects.all()}
    reasons = loop_block_reasons(now, rows=rows)
    return tuple(
        LoopRow(
            name=name,
            last_run_at=rows[name].last_run_at if name in rows else None,
            next_run_at=rows[name].next_run_at() if name in rows else None,
            cadence_label=rows[name].cadence_label if name in rows else "",
            blocked_reason=reason,
        )
        for name, reason in sorted(reasons.items())
    )


def _outcomes() -> tuple[OutcomeRow, ...]:
    attempts = (
        TaskAttempt.objects.filter(ended_at__isnull=False)
        .select_related("task", "task__ticket")
        .order_by("-ended_at", "-pk")[:LIVE_OUTCOME_ROWS]
    )
    return tuple(_outcome_row(attempt) for attempt in attempts)


def _outcome_row(attempt: TaskAttempt) -> OutcomeRow:
    ticket = attempt.task.ticket
    parked = attempt.error.startswith(LIMIT_PARKED_PREFIX)
    return OutcomeRow(
        attempt_id=attempt.pk,
        ticket_number=ticket.ticket_number if ticket else "",
        phase=attempt.task.phase,
        outcome=attempt.get_outcome_display() if attempt.outcome else "",  # ty: ignore[unresolved-attribute]
        exit_code=attempt.exit_code,
        ended_at=attempt.ended_at,
        duration=_duration(attempt),
        parked=parked,
        park_repeats=attempt.park_repeats,
    )


def _duration(attempt: TaskAttempt) -> str:
    if attempt.ended_at is None:
        return ""
    return _humanize_duration((attempt.ended_at - attempt.started_at).total_seconds())
