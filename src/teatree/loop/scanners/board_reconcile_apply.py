"""How one board-reconcile rule APPLIES its verdict — the machinery every rule shares.

Its own module so ``board_reconcile`` stays the rules and their forge reads, and so a
rule living in a sibling module (rule F, ``board_reconcile_issue_close``) reaches the
same application semantics without importing the host back and creating a cycle.
"""

import logging
from typing import TYPE_CHECKING

from teatree.core.models.errors import InvalidTransitionError
from teatree.core.models.merge_void import MergeVoid
from teatree.core.models.pull_request import PullRequest
from teatree.loop.scanners.board_reconcile_report import BoardAction, BoardTransition
from teatree.url_classify import pr_ref

if TYPE_CHECKING:
    from collections.abc import Callable, Iterable

    from django.db.models import QuerySet

    from teatree.core.models import Ticket
    from teatree.core.models.merge_void import VoidedRefs

logger = logging.getLogger(__name__)


def scoped(queryset: "QuerySet[Ticket]", overlay: str) -> "QuerySet[Ticket]":
    return queryset.filter(overlay=overlay) if overlay else queryset


def without_voided(tickets: "Iterable[Ticket]") -> "list[Ticket]":
    """Drop the tickets a merge void holds, so no rule re-merges, delivers or retires them from a voided PR."""
    voided = MergeVoid.objects.voided_refs()
    if not voided.refs:
        return list(tickets)
    restored = set(MergeVoid.objects.exclude(tickets=None).values_list("tickets", flat=True))
    return [ticket for ticket in tickets if not _held_by_void(ticket, voided, restored=restored)]


def _held_by_void(ticket: "Ticket", voided: "VoidedRefs", *, restored: set[int]) -> bool:
    """Its own url is a voided PR, or a void restored it and no PR outside the voided set has replaced it since."""
    if _voided_url(ticket.issue_url, voided):
        return True
    return ticket.pk in restored and all(
        _voided_url(url, voided) for url in PullRequest.objects.filter(ticket=ticket).values_list("url", flat=True)
    )


def _voided_url(url: str, voided: "VoidedRefs") -> bool:
    ref = pr_ref(url or "")
    return ref is not None and voided.covers(slug=ref.slug, pr_id=ref.pr_id, host_kind=ref.host_kind)


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
