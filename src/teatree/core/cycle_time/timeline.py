"""Per-ticket timeline: where the time went, split into waiting and working (#3847).

The split is the point. A phase that is slow because nothing was dispatched to it and
a phase that is slow because an agent ground on it for an hour are different problems,
and a view that merges them tells you only that the ticket was slow.

Both halves are read from stamps that mean what they say — ``Task.admitted_at`` (the
runner handoff, migration 0056) and ``TaskAttempt.ended_at`` (agent completion) — while
the phase boundary comes from :mod:`teatree.core.cycle_time.spans`. Nothing here reads
``TaskAttempt.started_at``: it is stamped at INSERT, which happens at completion, so it
is a second completion timestamp wearing a start's name.
"""

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime

from teatree.core.cost import AttemptUsage, CostBreakdown
from teatree.core.cycle_time.spans import PhaseSpan, spans_for_tickets
from teatree.core.cycle_time.work_intervals import Engagement, merged_seconds
from teatree.core.modelkit.phases import normalize_phase
from teatree.core.models.task import Task
from teatree.core.models.task_attempt import TaskAttempt
from teatree.core.models.ticket import Ticket


@dataclass(frozen=True, slots=True)
class PhaseSegment:
    """One span of a ticket's life, costed and split into queue wait versus work."""

    from_state: str
    to_state: str
    #: The phase whose output is *to_state* (``"coding"``, …), or ``""`` off the ladder.
    phase: str
    entered_at: datetime
    left_at: datetime
    seconds: float
    queue_seconds: float
    work_seconds: float
    #: False when an agent demonstrably RAN in this span (attempts ended in it) yet no
    #: task of the phase carries an admission stamp to measure the stretch with.
    #: ``Task.admitted_at`` is stamped going forward only, so a span that predates it
    #: has no honest split — reporting it as "0s working, all queue wait" would be the
    #: same fiction as subtracting the attempt stamps, arriving by a different route.
    #: ``work_seconds`` is 0.0 there and means UNKNOWN, not zero.
    work_measured: bool
    cost_usd: float
    #: How much of ``cost_usd`` is a price-table ESTIMATE rather than a reported figure.
    cost_estimated_usd: float
    attempts: int


@dataclass(frozen=True, slots=True)
class TicketTimeline:
    ticket_id: int
    number: str
    state: str
    started_at: datetime | None
    ended_at: datetime | None
    segments: tuple[PhaseSegment, ...]
    lead_time_seconds: float
    queue_seconds: float
    work_seconds: float
    #: True only when EVERY segment's split is measurable. One unmeasured phase leaves
    #: BOTH totals a lower bound (it contributes 0 to each rather than guessing), so
    #: they no longer add up to ``lead_time_seconds`` — which is why the flag rides
    #: alongside them instead of the difference being read as waiting.
    work_measured: bool
    cost_usd: float
    cost_estimated_usd: float

    @classmethod
    def empty(cls, ticket: Ticket) -> "TicketTimeline":
        """A ticket whose history has no measurable span yet — never a raise."""
        return cls(
            ticket_id=ticket.pk,
            number=ticket.ticket_number,
            state=str(ticket.state),
            started_at=None,
            ended_at=None,
            segments=(),
            lead_time_seconds=0.0,
            queue_seconds=0.0,
            work_seconds=0.0,
            work_measured=True,
            cost_usd=0.0,
            cost_estimated_usd=0.0,
        )


def build_ticket_timeline(ticket_id: int) -> TicketTimeline:
    """The one ticket's timeline — the answer to "where is this ticket spending its time"."""
    return build_ticket_timelines([ticket_id])[ticket_id]


def build_ticket_timelines(ticket_ids: Sequence[int]) -> dict[int, TicketTimeline]:
    """Timelines for a whole set, in a query plan that does not grow with the set.

    The dashboard renders one bar per ticket, so a per-ticket read here would be an
    N+1 the poll pays every time. Every model this needs is read once for the batch.
    """
    tickets = {ticket.pk: ticket for ticket in Ticket.objects.filter(pk__in=ticket_ids)}
    spans = spans_for_tickets(list(tickets))
    engagements = _engagements(list(tickets))
    usages = _phase_usages(list(tickets))
    return {
        ticket_id: _timeline(ticket, spans.get(ticket_id, ()), engagements, usages)
        for ticket_id, ticket in tickets.items()
    }


def _timeline(
    ticket: Ticket,
    spans: tuple[PhaseSpan, ...],
    engagements: dict[tuple[int, str], list[Engagement]],
    usages: dict[tuple[int, str], list[tuple[datetime | None, AttemptUsage]]],
) -> TicketTimeline:
    if not spans:
        return TicketTimeline.empty(ticket)
    segments = tuple(_segment(span, engagements, usages) for span in spans)
    return TicketTimeline(
        ticket_id=ticket.pk,
        number=ticket.ticket_number,
        state=str(ticket.state),
        started_at=spans[0].entered_at,
        ended_at=spans[-1].left_at,
        segments=segments,
        lead_time_seconds=sum(segment.seconds for segment in segments),
        queue_seconds=sum(segment.queue_seconds for segment in segments),
        work_seconds=sum(segment.work_seconds for segment in segments),
        work_measured=all(segment.work_measured for segment in segments),
        cost_usd=sum(segment.cost_usd for segment in segments),
        cost_estimated_usd=sum(segment.cost_estimated_usd for segment in segments),
    )


def _segment(
    span: PhaseSpan,
    engagements: dict[tuple[int, str], list[Engagement]],
    usages: dict[tuple[int, str], list[tuple[datetime | None, AttemptUsage]]],
) -> PhaseSegment:
    phase = Ticket.phase_producing_state(span.to_state)
    key = (span.ticket_id, phase)
    work = merged_seconds(engagements.get(key, ()), start=span.entered_at, end=span.left_at)
    priced = [
        usage
        for ended_at, usage in usages.get(key, ())
        if ended_at is not None and span.entered_at <= ended_at <= span.left_at
    ]
    # An agent demonstrably ran here iff an attempt ENDED here. With no measured
    # engagement to set against that, the split is unknown rather than zero.
    measured = work > 0.0 or not priced
    breakdown = CostBreakdown.from_usages(priced)
    return PhaseSegment(
        from_state=span.from_state,
        to_state=span.to_state,
        phase=phase,
        entered_at=span.entered_at,
        left_at=span.left_at,
        seconds=span.seconds,
        queue_seconds=max(0.0, span.seconds - work) if measured else 0.0,
        work_seconds=work,
        work_measured=measured,
        cost_usd=breakdown.total_usd,
        cost_estimated_usd=breakdown.estimated_usd,
        attempts=len(priced),
    )


def _engagements(ticket_ids: Sequence[int]) -> dict[tuple[int, str], list[Engagement]]:
    """Per (ticket, phase), each admitted task's stretch of agent engagement.

    A task's stretch opens at its admission stamp and closes at its LAST attempt's
    ``ended_at``. A task admitted but carrying no completed attempt is still engaged —
    ``None`` leaves it open, and the segment's own end closes it.
    """
    last_end = _last_attempt_end(ticket_ids)
    admitted = Task.objects.filter(ticket_id__in=ticket_ids, admitted_at__isnull=False).values_list(
        "pk",
        "ticket_id",
        "phase",
        "admitted_at",
    )
    grouped: dict[tuple[int, str], list[Engagement]] = {}
    for task_pk, ticket_id, phase, admitted_at in admitted:
        key = (ticket_id, normalize_phase(phase))
        grouped.setdefault(key, []).append(Engagement(start=admitted_at, end=last_end.get(task_pk)))
    return grouped


def _last_attempt_end(ticket_ids: Sequence[int]) -> dict[int, datetime]:
    ends: dict[int, datetime] = {}
    rows = TaskAttempt.objects.filter(task__ticket_id__in=ticket_ids, ended_at__isnull=False).values_list(
        "task_id",
        "ended_at",
    )
    for task_id, ended_at in rows:
        known = ends.get(task_id)
        ends[task_id] = ended_at if known is None or ended_at > known else known
    return ends


def _phase_usages(ticket_ids: Sequence[int]) -> dict[tuple[int, str], list[tuple[datetime | None, AttemptUsage]]]:
    """Each attempt's costing record, keyed by the (ticket, phase) that ran it.

    Priced through the shared :class:`~teatree.core.cost.AttemptUsage` seam rather than
    by summing ``cost_usd``: a row that reported no cost is priced from the token
    counts, and ``cost_is_estimated`` is what keeps a price-table guess distinguishable
    from a reported figure downstream.
    """
    rows = TaskAttempt.objects.filter(task__ticket_id__in=ticket_ids).values_list(
        "task__ticket_id",
        "task__phase",
        "ended_at",
        "model",
        "cost_usd",
        "cost_is_estimated",
        "lane",
        "input_tokens",
        "output_tokens",
        "cache_read_tokens",
        "cache_write_tokens",
    )
    grouped: dict[tuple[int, str], list[tuple[datetime | None, AttemptUsage]]] = {}
    for ticket_id, phase, ended_at, model, cost, estimated, lane, inp, out, cache_read, cache_write in rows:
        normalized = normalize_phase(phase)
        usage = AttemptUsage(
            model=model or None,
            reported_cost_usd=cost,
            input_tokens=inp or 0,
            output_tokens=out or 0,
            cache_read_tokens=cache_read or 0,
            cache_write_tokens=cache_write or 0,
            lane=lane,
            estimated=estimated,
            phase=normalized,
        )
        grouped.setdefault((ticket_id, normalized), []).append((ended_at, usage))
    return grouped
