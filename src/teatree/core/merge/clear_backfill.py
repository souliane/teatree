"""Recover the ticket link on ``MergeClear`` rows that were issued without one.

``--ticket-id`` on ``t3 <overlay> ticket clear`` is optional and no caller passed it,
so the keystone's post hook found ``clear.ticket is None`` and returned without
advancing anything — a merge landed and no card moved. The link is recoverable from
the PR itself, which is what this walk restores, together with the two facts the
existing drivers key on: the ``PullRequest`` row's MERGED state (``board_reconcile``
rule A) and the ticket's own FSM state.

Scope is deliberate. Only a CONSUMED CLEAR is linked: its merge already happened, so
the link is pure bookkeeping. An UNCONSUMED CLEAR is a live authorisation whose merge
gates read ``clear.ticket`` (anti-vacuity, rubric) — retro-fitting a ticket onto it
would change what may merge, so it is reported and left alone.

The ticket is advanced only for a CLEAR carrying its own ``MergeAudit``. A CLEAR
consumed as a superseded §15 sibling never merged anything, and the ``merge_evidence``
gate's keystone-artifact path is exactly that audit row — so the advance needs no
forge probe, and a gate refusal is reported rather than raised.
"""

from dataclasses import dataclass
from enum import StrEnum
from typing import TYPE_CHECKING

from django_fsm import TransitionNotAllowed

from teatree.core.models import MergeAudit, MergeClear, PullRequest
from teatree.core.models.errors import InvalidTransitionError

if TYPE_CHECKING:
    from teatree.core.models import Ticket


class BackfillOutcome(StrEnum):
    LINKED = "linked"
    UNRESOLVED = "unresolved"
    LIVE = "live"


@dataclass(frozen=True, slots=True)
class ClearBackfillRow:
    clear_id: int
    slug: str
    pr_id: int
    outcome: BackfillOutcome
    detail: str
    ticket_id: int = 0
    advanced_to: str = ""

    def line(self, *, dry_run: bool) -> str:
        prefix = "  [dry-run] " if dry_run else "  "
        ref = f"CLEAR {self.clear_id} {self.slug}#{self.pr_id}"
        if self.outcome is BackfillOutcome.LINKED:
            landing = f" → {self.advanced_to}" if self.advanced_to else ""
            return f"{prefix}{ref}: ticket #{self.ticket_id}{landing} ({self.detail})"
        return f"{prefix}{ref}: {self.outcome} — {self.detail}"


@dataclass(frozen=True, slots=True)
class ClearBackfillReport:
    rows: tuple[ClearBackfillRow, ...]
    dry_run: bool

    def of(self, outcome: BackfillOutcome) -> tuple[ClearBackfillRow, ...]:
        return tuple(row for row in self.rows if row.outcome is outcome)

    def lines(self) -> list[str]:
        if not self.rows:
            return ["Every MergeClear already carries its ticket."]
        verb = "would be" if self.dry_run else "were"
        summary = (
            f"{len(self.of(BackfillOutcome.LINKED))} CLEAR(s) {verb} linked; "
            f"{len(self.of(BackfillOutcome.UNRESOLVED))} unresolvable; "
            f"{len(self.of(BackfillOutcome.LIVE))} skipped as live authorisations."
        )
        return [*[row.line(dry_run=self.dry_run) for row in self.rows], "", summary]


def _row(
    clear: MergeClear,
    outcome: BackfillOutcome,
    detail: str,
    *,
    ticket_id: int = 0,
    advanced_to: str = "",
) -> ClearBackfillRow:
    return ClearBackfillRow(
        clear_id=int(clear.pk),
        slug=clear.slug,
        pr_id=clear.pr_id,
        outcome=outcome,
        detail=detail,
        ticket_id=ticket_id,
        advanced_to=advanced_to,
    )


def _advance_ticket(clear: MergeClear, ticket: "Ticket") -> tuple[str, str]:
    """Reconcile *ticket* toward MERGED; return ``(landed_state, detail)``."""
    if not MergeAudit.objects.filter(clear=clear).exists():
        return "", "linked (no merge audit on this CLEAR — superseded sibling)"
    try:
        ticket.reconcile_merged()
    except TransitionNotAllowed:
        return "", f"linked (ticket already past MERGED at {ticket.state})"
    except InvalidTransitionError as exc:
        return "", f"linked (ticket held at {ticket.state}: {exc})"
    ticket.save()
    return ticket.state, "linked and advanced"


def backfill_clear_tickets(*, dry_run: bool = False) -> ClearBackfillReport:
    rows: list[ClearBackfillRow] = []
    for clear in MergeClear.objects.filter(ticket__isnull=True).order_by("pk"):
        if clear.consumed_at is None:
            detail = "unconsumed CLEAR — linking it would change the merge gates it faces"
            rows.append(_row(clear, BackfillOutcome.LIVE, detail))
            continue
        ticket = PullRequest.objects.owning_ticket(slug=clear.slug, pr_id=clear.pr_id)
        if ticket is None:
            detail = "no PullRequest row for the PR and no ticket carrying it in extra['prs']"
            rows.append(_row(clear, BackfillOutcome.UNRESOLVED, detail))
            continue
        if dry_run:
            rows.append(_row(clear, BackfillOutcome.LINKED, "resolved via the PR", ticket_id=int(ticket.pk)))
            continue
        clear.adopt_owning_ticket()
        PullRequest.objects.record_forge_merge(slug=clear.slug, pr_id=clear.pr_id)
        landed, detail = _advance_ticket(clear, ticket)
        rows.append(
            _row(clear, BackfillOutcome.LINKED, detail, ticket_id=int(ticket.pk), advanced_to=landed),
        )
    return ClearBackfillReport(rows=tuple(rows), dry_run=dry_run)
