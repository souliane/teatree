"""The board reconcile's report vocabulary — one ticket's outcome and a run's record.

Its own module so ``board_reconcile`` stays the rules and their forge reads, and so the
CLI sweep can render a report without importing the janitor's internals.
"""

from dataclasses import dataclass
from enum import StrEnum


class BoardAction(StrEnum):
    """What the reconcile did to one ticket.

    ``REFUSED`` is the walk an FSM gate stopped short — reported, never swallowed,
    with whatever partial progress persisted before the refusal.
    """

    ADVANCED_MERGED = "advanced_merged"
    ADVANCED_DELIVERED = "advanced_delivered"
    IGNORED_CLOSED = "ignored_closed"
    REVIEW_CLOSED = "review_closed"
    REVIVED_REOPENED = "revived_reopened"
    REFUSED = "refused"


@dataclass(frozen=True, slots=True)
class BoardTransition:
    """One ticket's reconciliation outcome — what changed, and on what evidence."""

    ticket_id: int
    issue_url: str
    from_state: str
    to_state: str
    action: BoardAction
    reason: str
    applied: bool
    error: str = ""

    def line(self) -> str:
        if self.action is BoardAction.REFUSED:
            landing = f"{self.to_state} " if self.to_state != self.from_state else ""
            return f"  #{self.ticket_id} {self.from_state} → {landing}refused: {self.error}"
        prefix = "  " if self.applied else "  [dry-run] "
        return f"{prefix}#{self.ticket_id} {self.from_state} → {self.to_state} ({self.reason})"


@dataclass(frozen=True, slots=True)
class BoardReconcileReport:
    """What one reconcile run changed, why, and how much forge work it spent."""

    transitions: tuple[BoardTransition, ...]
    probes: int
    dry_run: bool

    @property
    def applied(self) -> tuple[BoardTransition, ...]:
        return tuple(t for t in self.transitions if t.applied)

    @property
    def refused(self) -> tuple[BoardTransition, ...]:
        return tuple(t for t in self.transitions if t.action is BoardAction.REFUSED)

    def lines(self) -> list[str]:
        """The human-readable record — the janitor's own evidence, not the tick's."""
        if not self.transitions:
            return ["Board already reconciled — nothing to advance."]
        moved = [t for t in self.transitions if t.action is not BoardAction.REFUSED]
        verb = "would be reconciled" if self.dry_run else "reconciled"
        lines = [t.line() for t in self.transitions]
        if moved:
            lines.append(f"{len(moved)} ticket(s) {verb}.")
        if self.refused:
            lines.append(f"{len(self.refused)} ticket(s) skipped (gate-refused).")
        return lines
