"""The one derivation of "how long did this ticket spend here" (#3847).

A ticket's history is a chain of :class:`~teatree.core.models.transition.TicketTransition`
rows, so the time an edge ``X -> Y`` took is the gap between arriving at ``X`` and the
row recording the move to ``Y``. That gap is the ONLY duration this codebase may
compute: ``TaskAttempt.started_at`` and ``ended_at`` are both stamped when the row is
inserted at agent completion, so their difference is ~0 and a duration built on it is
fiction.

The chain is walked over ``state_edges()`` — a ``from_state == to_state`` row records
no move, and counting it would split one real span into two shorter ones.
"""

from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from datetime import datetime
from itertools import pairwise

from django.db.models import QuerySet

from teatree.core.models.transition import TicketTransition


@dataclass(frozen=True, slots=True)
class PhaseSpan:
    """One measured edge: the ticket sat in *from_state* until it moved to *to_state*."""

    ticket_id: int
    from_state: str
    to_state: str
    entered_at: datetime
    left_at: datetime

    @property
    def seconds(self) -> float:
        return (self.left_at - self.entered_at).total_seconds()


def spans_for_tickets(ticket_ids: Sequence[int]) -> dict[int, tuple[PhaseSpan, ...]]:
    """Every measured span of each ticket, oldest-first — one query for the whole set."""
    if not ticket_ids:
        return {}
    return _group_by_ticket(TicketTransition.objects.filter(ticket_id__in=ticket_ids))


def spans_since(since: datetime, until: datetime | None = None) -> tuple[PhaseSpan, ...]:
    """Every span that FINISHED in the window, across every ticket that moved in it.

    A span is placed by where it ENDED, not where it began: the whale spans are long,
    so dropping one for having started before the window is exactly how a tail
    disappears from the aggregate. Reaching the predecessor of the window's first
    in-window transition is why the candidate set is "tickets that moved", not
    "transitions in the window".
    """
    moved = TicketTransition.objects.filter(created_at__gte=since).values("ticket_id")
    if until is not None:
        moved = moved.filter(created_at__lte=until)
    by_ticket = _group_by_ticket(TicketTransition.objects.filter(ticket_id__in=moved))
    return tuple(
        span
        for spans in by_ticket.values()
        for span in spans
        if span.left_at >= since and (until is None or span.left_at <= until)
    )


def _group_by_ticket(rows: QuerySet[TicketTransition]) -> dict[int, tuple[PhaseSpan, ...]]:
    ordered = (
        rows.state_edges()
        .order_by("ticket_id", "created_at", "pk")
        .values_list(
            "ticket_id",
            "from_state",
            "to_state",
            "created_at",
        )
    )
    per_ticket: dict[int, list[tuple[int, str, str, datetime]]] = {}
    for row in ordered:
        per_ticket.setdefault(row[0], []).append(row)
    return {ticket_id: tuple(_pairwise_spans(chain)) for ticket_id, chain in per_ticket.items()}


def _pairwise_spans(chain: Sequence[tuple[int, str, str, datetime]]) -> Iterable[PhaseSpan]:
    """Each transition measured against its predecessor's timestamp.

    The FIRST transition opens the timeline and measures nothing: ``Ticket`` carries no
    creation stamp, so there is no honest instant to subtract from it.
    """
    for previous, current in pairwise(chain):
        ticket_id, _, to_state, created_at = current
        yield PhaseSpan(
            ticket_id=ticket_id,
            from_state=current[1],
            to_state=to_state,
            entered_at=previous[3],
            left_at=created_at,
        )
