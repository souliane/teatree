"""The board janitor's hold on a merge that leaves an item open (#5670): rules A and B, and rule G.

Rules A and B settle a ticket from a merged pull request, so they ask the same question the merge
keystone asks — :func:`~teatree.core.merge.partial_merge.blocks_settlement` — before they move it.
Rule G is the backstop for what already settled: it re-reads the last merged pull request of a
settled ticket whose issue is open and sends the ticket back to planning when that merge left an
item out, a bounded batch per run, least recently checked first.
"""

import logging
from dataclasses import dataclass

from django.db.models import F
from django.utils import timezone

from teatree.core.merge.partial_merge import blocks_settlement, read_last_merged_pr
from teatree.core.merge.partial_merge_reconcile import unsettle_ticket
from teatree.core.models import Ticket
from teatree.loop.scanners.board_reconcile_apply import collect, scoped, without_voided
from teatree.loop.scanners.board_reconcile_report import BoardAction, BoardTransition

logger = logging.getLogger(__name__)

#: Settled tickets rule G inspects per run, sized so the standing backlog drains in a day of hourly runs.
SETTLED_INSPECTION_CAP = 20


@dataclass(slots=True)
class ReadBudget:
    """The forge reads a board run may still spend on judging a merge, one per ticket."""

    left: int
    used: int = 0

    def take(self) -> bool:
        if self.left <= 0:
            return False
        self.left -= 1
        self.used += 1
        return True


def _refused(ticket: Ticket, *, from_state: str, reason: str, error: str) -> BoardTransition:
    return BoardTransition(
        ticket_id=int(ticket.pk),
        issue_url=ticket.issue_url,
        from_state=from_state,
        to_state=from_state,
        action=BoardAction.REFUSED,
        reason=reason,
        applied=False,
        error=error,
    )


def _held(ticket: Ticket, *, from_state: str, to_state: str, reason: str, applied: bool) -> BoardTransition:
    return BoardTransition(
        ticket_id=int(ticket.pk),
        issue_url=ticket.issue_url,
        from_state=from_state,
        to_state=to_state,
        action=BoardAction.HELD_FOR_REMAINDER,
        reason=reason,
        applied=applied,
    )


def settlement_block(ticket: Ticket, *, reason: str, dry_run: bool) -> BoardTransition | None:
    """The row for a ticket the merge must not settle, or ``None`` when it may move to ``merged``."""
    reading, merged_pr = read_last_merged_pr(ticket)
    from_state = ticket.state
    if not blocks_settlement(ticket, reading, merged_pr=merged_pr or ticket.issue_url, dry_run=dry_run):
        return None
    if reading.unreadable:
        return _refused(ticket, from_state=from_state, reason=reason, error=reading.unreadable)
    return _held(
        ticket,
        from_state=from_state,
        to_state=Ticket.State.WORK_STARTED if dry_run else ticket.state,
        reason=f"{reason}; {len(reading.unmet)} item(s) left open",
        applied=not dry_run,
    )


def _settled_candidates(overlay: str, already_moved: frozenset[int] = frozenset()) -> list[Ticket]:
    return without_voided(
        scoped(
            Ticket.objects.filter(
                state__in=Ticket.merged_states() - {Ticket.State.DELIVERED},
                role=Ticket.Role.AUTHOR,
                remote_missing=False,
            )
            .exclude(repo_namespaced_key="")
            .exclude(pk__in=already_moved)
            .order_by(F("extra__settled_checked_at").asc(nulls_first=True), "pk"),
            overlay,
        )
    )


def settled_probe_reservation(overlay: str, remaining: int) -> int:
    """Probes held back for rule G before E and F split the rest: a quarter, at most one batch, only what it can use."""
    return min(SETTLED_INSPECTION_CAP, max(remaining, 0) // 4, len(_settled_candidates(overlay)))


def settled_remainder_transitions(
    *, overlay: str, dry_run: bool, probe_budget: int, already_moved: frozenset[int]
) -> tuple[list[BoardTransition], int]:
    """Rule G — author tickets at ``merged`` or ``retro_recorded`` whose open issue the last merge left items on.

    ``delivered`` is excluded: it passed the rubric gate. Every inspected ticket is stamped
    with ``settled_checked_at`` first, so the batch rotates through the whole backlog instead of
    re-reading the same head of it each run.
    """
    inspected = _settled_candidates(overlay, already_moved)[: max(min(probe_budget, SETTLED_INSPECTION_CAP), 0)]
    return collect(inspected, lambda ticket: _unsettle(ticket, dry_run=dry_run)), len(inspected)


def _unsettle(ticket: Ticket, *, dry_run: bool) -> BoardTransition | None:
    reason = "settled with the issue open"
    if not dry_run:
        ticket.merge_extra(set_keys={"settled_checked_at": timezone.now().isoformat()})
    result = unsettle_ticket(int(ticket.pk), reason="", dry_run=dry_run)
    if result.get("unreadable"):
        return _refused(ticket, from_state=ticket.state, reason=reason, error=result["error"])
    if "error" in result:
        return None
    logger.info("Board reconcile unsettled ticket %s from %s (%s)", ticket.pk, ticket.state, result["remainder"])
    return _held(
        ticket,
        from_state=ticket.state,
        to_state=Ticket.State.WORK_STARTED if dry_run else result["state"],
        reason=f"{reason}; the last merge left {len(result['remainder'])} item(s) open",
        applied=not dry_run,
    )
