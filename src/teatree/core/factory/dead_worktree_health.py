"""Health signals for dead worktree rows that the safe reaper keeps."""

from teatree.config import clone_root
from teatree.core.factory.health_signal import HealthSignal, SignalCollection
from teatree.core.models.known_issue import KnownIssue
from teatree.core.worktree.dead_row_release import plan_dead_row_release


def dead_worktree_row_signals() -> SignalCollection:
    return SignalCollection(
        tuple(
            HealthSignal(
                fingerprint=f"dead-worktree:{row.worktree_pk}:{row.disposition}",
                severity=KnownIssue.Severity.WARNING,
                kind="dead_worktree_row",
                summary=f"worktree row {row.worktree_pk} for {row.branch} kept: {row.reason}",
            )
            for row in plan_dead_row_release(clone_root())
            if not row.releasable
        )
    )
