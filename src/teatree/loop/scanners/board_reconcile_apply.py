"""How one board-reconcile rule APPLIES its verdict — the machinery every rule shares.

Its own module so ``board_reconcile`` stays the rules and their forge reads, and so a
rule living in a sibling module (rule F, ``board_reconcile_issue_close``) reaches the
same application semantics without importing the host back and creating a cycle.
"""

import logging
from typing import TYPE_CHECKING

from teatree.core.models.errors import InvalidTransitionError
from teatree.loop.scanners.board_reconcile_report import BoardAction, BoardTransition

if TYPE_CHECKING:
    from collections.abc import Callable, Iterable

    from django.db.models import QuerySet

    from teatree.core.models import Ticket

logger = logging.getLogger(__name__)


def scoped(queryset: "QuerySet[Ticket]", overlay: str) -> "QuerySet[Ticket]":
    return queryset.filter(overlay=overlay) if overlay else queryset


def collect(
    tickets: "Iterable[Ticket]",
    reconcile_one: "Callable[[Ticket], BoardTransition | None]",
) -> list[BoardTransition]:
    """Apply *reconcile_one* per ticket, isolating each row from the others.

    A gate refusal (the ``merge_evidence`` fail-closed path) and an unexpected
    per-row error are both logged and skipped — one poison ticket must never abort a
    whole-table sweep.
    """
    transitions: list[BoardTransition] = []
    for ticket in tickets:
        try:
            transition = reconcile_one(ticket)
        except InvalidTransitionError as exc:
            logger.debug("Board reconcile skipped ticket %s — gate refused: %s", ticket.pk, exc)
        except Exception:
            logger.exception("Board reconcile skipped ticket %s after an unexpected error", ticket.pk)
        else:
            if transition is not None:
                transitions.append(transition)
    return transitions


def planned(ticket: "Ticket", to_state: str, action: BoardAction, reason: str) -> BoardTransition:
    return BoardTransition(
        ticket_id=int(ticket.pk),
        issue_url=ticket.issue_url,
        from_state=ticket.state,
        to_state=to_state,
        action=action,
        reason=reason,
        applied=False,
    )
