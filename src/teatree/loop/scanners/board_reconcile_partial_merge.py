"""The board janitor's hold on a merge that leaves an item open (#5670): rules A and B, and rule G.

Rules A and B settle a ticket from a merged pull request, so they ask the same question the merge
keystone asks — :func:`~teatree.core.merge.partial_merge.blocks_settlement` — before they move it.
Rule G is the backstop for what already settled: it re-reads the last merged pull request of a
settled ticket whose issue is open and sends the ticket back to planning when that merge left an
item out, a bounded batch per run, least recently checked first.

Every forge read of these rules goes through ``board_reconcile_reads`` — concurrent, and stopped
before the scan deadline — while the database lookups stay on the calling thread.
"""

import logging
from collections.abc import Iterable
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from django.db.models import Exists, F, OuterRef
from django.utils import timezone
from django_fsm import can_proceed

from teatree.core.gates.issue_coverage import holds_on_issue
from teatree.core.models import PullRequest, Ticket
from teatree.loop.scanners.board_reconcile_apply import collect, scoped, without_voided
from teatree.loop.scanners.board_reconcile_reads import read_concurrently
from teatree.loop.scanners.board_reconcile_report import BoardAction, BoardTransition

if TYPE_CHECKING:
    from teatree.core.merge.last_merged_pr import LastMergedPr
    from teatree.core.merge.partial_merge import MergedPrReading
    from teatree.core.merge.partial_merge_report import SettledTicketInspection

logger = logging.getLogger(__name__)

#: Settled tickets rule G inspects per run, sized so the standing backlog drains in a day of hourly runs.
SETTLED_INSPECTION_CAP = 20


@dataclass(slots=True)
class MergeReads:
    """The merged pull requests rules A and B judge, read together before either applies a verdict.

    One read per ticket, charged to the run's probe budget once it completed: a read the scan
    deadline stopped leaves its ticket for the next run.
    """

    left: int
    used: int = 0
    readings: "dict[int, tuple[MergedPrReading, str]]" = field(default_factory=dict)

    def read(self, tickets: Iterable[Ticket]) -> None:
        from teatree.core.merge.partial_merge import read_merged_pr_at  # noqa: PLC0415 — deferred: off the tick import

        wanted = [
            ticket
            for ticket in tickets
            if holds_on_issue(ticket) and ticket.state != Ticket.State.MERGED and can_proceed(ticket.reconcile_merged)
        ][: max(self.left, 0)]
        latest = _last_merged_prs(wanted)
        for ticket, reading in read_concurrently(wanted, lambda ticket: read_merged_pr_at(ticket, latest[ticket.pk])):
            self.readings[ticket.pk] = reading
            self.left -= 1
            self.used += 1

    def reading_of(self, ticket: Ticket) -> "tuple[MergedPrReading, str] | None":
        return self.readings.get(ticket.pk)


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


def settlement_block(
    ticket: Ticket, read: "tuple[MergedPrReading, str]", *, reason: str, dry_run: bool
) -> BoardTransition | None:
    """The row for a ticket the merge must not settle, or ``None`` when it may move to ``merged``."""
    from teatree.core.merge.partial_merge import blocks_settlement  # noqa: PLC0415 — deferred: off the tick import

    reading, merged_pr = read
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
                Exists(PullRequest.objects.filter(ticket=OuterRef("pk"), state=PullRequest.State.MERGED)),
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


def _last_merged_prs(tickets: Iterable[Ticket]) -> dict[int, "LastMergedPr | None"]:
    from teatree.core.merge.last_merged_pr import last_merged_pr  # noqa: PLC0415 — deferred: off the tick import

    return {ticket.pk: last_merged_pr(ticket) for ticket in tickets}


def settled_remainder_transitions(
    *, overlay: str, dry_run: bool, probe_budget: int, already_moved: frozenset[int]
) -> tuple[list[BoardTransition], int]:
    """Rule G — author tickets at ``merged`` or ``retro_recorded`` whose open issue the last merge left items on.

    ``delivered`` is excluded: it passed the rubric gate. Every ticket whose read completed is
    stamped with ``settled_checked_at`` first, so the batch rotates through the whole backlog
    instead of re-reading the same head of it each run; one the scan deadline stopped is neither
    judged, stamped nor counted.
    """
    from teatree.core.merge.partial_merge_report import read_settled_pr  # noqa: PLC0415 — deferred: off the tick import

    batch = _settled_candidates(overlay, already_moved)[: max(min(probe_budget, SETTLED_INSPECTION_CAP), 0)]
    latest = _last_merged_prs(batch)
    inspections = dict(read_concurrently(batch, lambda ticket: read_settled_pr(ticket, latest[ticket.pk])))
    transitions = collect(inspections, lambda ticket: _unsettle(ticket, inspections[ticket], dry_run=dry_run))
    return transitions, len(inspections)


def _unsettle(ticket: Ticket, inspection: "SettledTicketInspection", *, dry_run: bool) -> BoardTransition | None:
    from teatree.core.merge.partial_merge_reconcile import (  # noqa: PLC0415 — deferred: off the tick import
        unsettle_ticket,
    )

    reason = "settled with the issue open"
    if not dry_run:
        ticket.merge_extra(set_keys={"settled_checked_at": timezone.now().isoformat()})
    result = unsettle_ticket(int(ticket.pk), reason="", dry_run=dry_run, inspection=inspection)
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
