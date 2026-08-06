"""The dashboard's enqueue-a-task buttons: which phases, enabled, and why not (#4085).

The buttons enqueue WORK; nothing here can record an OUTCOME. Prioritising a review is
safe, asserting its result is not — no dashboard control writes a ``ReviewVerdict`` or a
``MergeClear``, pinned whole-package by
``tests/teatree_dash/test_no_review_outcome_writes.py``.

Two surfaces share one read model. The drawer renders the full phase set from a single
ticket's own tasks; the board card renders the two the owner reaches for, batched across
every card so the poll costs one query rather than one per card.
"""

from dataclasses import dataclass
from typing import TYPE_CHECKING

from teatree.core.models.task import Task
from teatree.core.models.ticket import Ticket

if TYPE_CHECKING:
    from collections.abc import Mapping

#: The phases a dashboard button may enqueue, in drawer order. A strict subset of
#: ``CANONICAL_PHASES``: the CLI accepts a free-form phase, the button surface does not.
ENQUEUEABLE_PHASES: tuple[str, ...] = ("scoping", "coding", "testing", "reviewing", "shipping")

#: What a board card offers. The card is a click-target that opens the drawer, so it
#: carries only the two phases the owner prioritises from the board — the rest stay one
#: click away rather than crowding every card.
BOARD_PHASES: tuple[str, ...] = ("reviewing", "shipping")

_LABELS: dict[str, str] = {
    "scoping": "Scope now",
    "coding": "Code now",
    "testing": "Test now",
    "reviewing": "Review now",
    "shipping": "Ship now",
}


@dataclass(frozen=True, slots=True)
class EnqueueButton:
    """One phase button. ``reason`` says why it is disabled, and is empty when it is not."""

    phase: str
    label: str
    enabled: bool
    reason: str


def enqueue_buttons(ticket: Ticket) -> tuple[EnqueueButton, ...]:
    """The drawer's button row for *ticket* — one bounded read of its unstarted tasks.

    A button whose phase already has an unstarted task is disabled naming that task,
    so a click that would be refused is never offered in the first place.
    """
    return _buttons(_oldest_pending_task_per_phase(ticket), ENQUEUEABLE_PHASES)


def board_enqueue_buttons(queued: "Mapping[str, int]") -> tuple[EnqueueButton, ...]:
    """A card's button row, built from *queued* — the card's slice of the batched read."""
    return _buttons(queued, BOARD_PHASES)


def pending_phase_tasks_by_ticket(ticket_ids: "list[int]") -> dict[int, dict[str, int]]:
    """Ticket pk -> its offered phases' oldest unstarted task pks, for the whole board.

    One query for every card, so the 4s board poll does not pay an N+1 for a row of
    buttons — the shape :func:`teatree.dash.selectors.build_kanban_columns` bulk-fetches
    every other per-card signal in.
    """
    if not ticket_ids:
        return {}
    rows = (
        Task.objects.filter(ticket_id__in=ticket_ids, phase__in=BOARD_PHASES, status=Task.Status.PENDING)
        .order_by("-pk")
        .values_list("ticket_id", "phase", "pk")
    )
    queued: dict[int, dict[str, int]] = {}
    for ticket_id, phase, task_pk in rows:
        queued.setdefault(ticket_id, {})[phase] = task_pk
    return queued


def _buttons(queued: "Mapping[str, int]", phases: tuple[str, ...]) -> tuple[EnqueueButton, ...]:
    return tuple(
        EnqueueButton(
            phase=phase,
            label=_LABELS[phase],
            enabled=phase not in queued,
            reason=f"TODO-{queued[phase]} is already queued for {phase}" if phase in queued else "",
        )
        for phase in phases
    )


def _oldest_pending_task_per_phase(ticket: Ticket) -> dict[str, int]:
    """Offered phase -> the pk of its oldest unstarted task, for the phases that have one.

    Descending pk so the dict ends up holding the OLDEST — the same row
    :func:`teatree.core.task_enqueue.enqueue_phase_task_once` refuses against, so the
    disabled button and the POST refusal can never name two different tasks.
    """
    rows = (
        ticket.tasks.filter(phase__in=ENQUEUEABLE_PHASES, status=Task.Status.PENDING)  # ty: ignore[unresolved-attribute]
        .order_by("-pk")
        .values_list("phase", "pk")
    )
    return dict(rows)
