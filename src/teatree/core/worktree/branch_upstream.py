"""The branch-upstream invariant across every clone teatree knows (#4225).

:mod:`teatree.utils.git_upstream` answers the question for one repo. Branch
config is per-CLONE and every worktree shares its clone's, so the sweep that
makes the invariant enforceable — the doctor check and the repair command — has
to walk the same clone set as the other clone-wide passes.
"""

from dataclasses import dataclass
from pathlib import Path

from teatree.config import clone_root
from teatree.core.worktree.clone_paths import known_clone_paths
from teatree.utils.git_upstream import BranchUpstream, mistracked_branches, repair_mistracked_branches


@dataclass(frozen=True)
class CloneMistracking:
    """One clone's mistracked branches, ready to report or repair."""

    clone: Path
    branches: list[BranchUpstream]

    def findings(self) -> list[str]:
        return [
            f"{self.clone}: branch '{entry.branch}' tracks {entry.merge_ref} — Fix: git -C {self.clone} "
            f"{entry.remedy.removeprefix('git ')}"
            for entry in self.branches
        ]


def scan_clones() -> list[CloneMistracking]:
    """Every known clone holding at least one mistracked branch, path-sorted."""
    found = (
        CloneMistracking(clone=clone, branches=mistracked_branches(str(clone)))
        for clone in sorted(known_clone_paths(clone_root()))
    )
    return [entry for entry in found if entry.branches]


def repair_clones(*, dry_run: bool = False) -> list[str]:
    """Repair every known clone's mistracked branches; ``[]`` when there is nothing to do."""
    lines: list[str] = []
    for clone in sorted(known_clone_paths(clone_root())):
        outcomes = repair_mistracked_branches(str(clone), dry_run=dry_run)
        lines.extend(f"{clone}: {outcome}" for outcome in outcomes)
    return lines


__all__ = ["CloneMistracking", "repair_clones", "scan_clones"]
