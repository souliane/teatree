"""Release only worktree registry rows whose checkout is proven dead."""

from dataclasses import dataclass

from teatree.config import clone_root
from teatree.core.worktree.dead_row_release import release_dead_rows
from teatree.loop.scanners.base import ScanSignal


@dataclass(slots=True)
class DeadWorktreeRowScanner:
    name: str = "dead_worktree_rows"

    @staticmethod
    def scan() -> list[ScanSignal]:
        released = release_dead_rows(clone_root(), dry_run=False).released_pks
        if not released:
            return []
        return [
            ScanSignal(
                kind="dead_worktree_rows.released",
                summary=f"released {len(released)} dead worktree registry row(s)",
                payload={"released": sorted(released)},
            )
        ]
