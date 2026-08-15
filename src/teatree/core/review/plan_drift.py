"""The whole-board plan-drift ratio the issue asked to surface (#4348).

The sibling :mod:`teatree.core.review.refix_plan` owns the GATE — what a review
HOLD is holding back right now. This module owns the DETECTOR, which is a wider
question and deliberately so: ``coding_tasks_since_last_plan > 1`` on an open
ticket names every ticket that re-implemented off one plan, held or not. The
measurement that opened the issue (177 coding-phase tasks against 29 planning,
repo-wide) counted exactly that population, and most of it never carried a HOLD —
so a detector restricted to the blocked set would restate the gate and report
nothing for the query it exists to answer.

Reads ``refix_plan``, never the reverse, so the gate stays free of the report.
"""

from dataclasses import dataclass
from datetime import datetime
from typing import Final, TypedDict

from django.db.models import Count, Q

from teatree.core.models.ticket import Ticket
from teatree.core.review.refix_plan import (
    IMPLEMENTING_PHASE_SPELLINGS,
    coding_tasks_since_last_plan,
    governing_plan_at,
    tickets_awaiting_refix_plan,
)

#: The issue's detector reads ``coding_tasks_since_last_plan > 1`` — one implementing
#: dispatch per plan is the healthy shape, so drift starts at the SECOND one.
_DRIFT_THRESHOLD: Final = 1

#: A ticket in one of these states has nothing left to re-implement, so its drift is
#: history rather than a live gap. The model's own done set, plus its post-merge
#: sibling — the pairing ``loop.stuck_ticket_redispatch`` already treats as terminal.
_CLOSED_STATES: Final[frozenset[str]] = Ticket.marker_release_states() | {Ticket.State.RETROSPECTED}


class PlanDriftRow(TypedDict):
    """The JSON-serialisable shape of one :class:`PlanDrift` (the CLI report)."""

    ticket_id: int
    issue_url: str
    state: str
    overlay: str
    plan_recorded_at: str
    coding_tasks_since_last_plan: int
    blocked: bool


@dataclass(frozen=True, slots=True)
class PlanDrift:
    """One open ticket that has re-implemented more than once off a single plan."""

    ticket_id: int
    issue_url: str
    state: str
    overlay: str
    plan_recorded_at: datetime | None
    coding_tasks_since_last_plan: int
    blocked: bool

    def as_row(self) -> PlanDriftRow:
        """This row as the report's serialisable shape; an absent plan is an empty stamp."""
        return PlanDriftRow(
            ticket_id=self.ticket_id,
            issue_url=self.issue_url,
            state=self.state,
            overlay=self.overlay,
            plan_recorded_at=self.plan_recorded_at.isoformat() if self.plan_recorded_at else "",
            coding_tasks_since_last_plan=self.coding_tasks_since_last_plan,
            blocked=self.blocked,
        )


def tickets_with_plan_drift(overlay: str = "") -> list[PlanDrift]:
    """Every OPEN author ticket whose implementing dispatches outnumber its plans.

    The overlap with the blocked set is reported rather than removed — ``blocked``
    marks the rows the gate already holds — because a difference of two sets is
    neither of the questions the reader is asking.

    Ordered by ticket pk so a report is stable across runs; scoped to *overlay* when
    given. Bounded by the tickets carrying more than one implementing task, never the
    board: the count filter is a necessary condition for the per-ticket comparison,
    so the pool is the candidates rather than every open ticket.
    """
    blocked = {held.ticket_id for held in tickets_awaiting_refix_plan(overlay=overlay)}
    candidates = (
        Ticket.objects.filter(role=Ticket.Role.AUTHOR)
        .exclude(state__in=_CLOSED_STATES)
        .annotate(implementing_tasks=Count("tasks", filter=Q(tasks__phase__in=IMPLEMENTING_PHASE_SPELLINGS)))
        .filter(implementing_tasks__gt=_DRIFT_THRESHOLD)
    )
    if overlay:
        candidates = candidates.filter(overlay=overlay)
    rows: list[PlanDrift] = []
    for ticket in candidates.order_by("pk"):
        since = coding_tasks_since_last_plan(ticket)
        if since <= _DRIFT_THRESHOLD:
            continue
        rows.append(
            PlanDrift(
                ticket_id=int(ticket.pk),
                issue_url=ticket.issue_url,
                state=str(ticket.state),
                overlay=ticket.overlay,
                plan_recorded_at=governing_plan_at(ticket),
                coding_tasks_since_last_plan=since,
                blocked=int(ticket.pk) in blocked,
            )
        )
    return rows
