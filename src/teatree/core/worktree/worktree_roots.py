"""The worktree ROOTS the reaper and the doctor scan — one canonical location (#3583).

Worktrees ended up split across two roots: the canonical per-overlay
:func:`teatree.config.worktree_root` that provisioning writes to, and whatever
ad-hoc root an agent happened to `git worktree add` into. A split namespace means
the reaper and `t3 doctor` each see half the picture, so broken checkouts pile up
in the half nobody scans and agents waste time deciding whether a stale sibling
elsewhere is live.

This module is the single answer to "which roots hold teatree worktrees?". The
canonical root is where new worktrees go; the scanned set additionally covers the
roots existing registered worktrees actually live in, so an alternate root is
DRAINED by the reaper rather than left to accumulate — and, once drained, never
written to again, collapsing the split without a manual migration.
"""

from pathlib import Path

from teatree.config import worktree_root
from teatree.core.models import Worktree
from teatree.utils import git
from teatree.utils.run import CommandFailedError


def resolves_as_git_checkout(path: Path) -> bool:
    """Whether ``git rev-parse`` succeeds inside *path* — the broken-checkout probe.

    The exact probe whose failure the setup-time ``is not a git checkout`` WARN
    reports, so the reaper, the doctor check and that warning can never disagree
    about which dirs are broken.
    """
    try:
        return bool(git.run(repo=str(path), args=["rev-parse", "--git-dir"]).strip())
    except (CommandFailedError, OSError):
        return False


def canonical_worktree_root() -> Path:
    """Where NEW worktrees are created — the one location everything converges on."""
    return worktree_root()


def registered_worktree_roots() -> set[Path]:
    """The parent dirs of every registered worktree's on-disk checkout.

    A row whose checkout sits outside :func:`canonical_worktree_root` contributes
    its own parent, which is how an alternate root becomes visible to the scans.
    """
    return {Path(wt.worktree_path).parent for wt in Worktree.objects.all() if wt.worktree_path}


def scanned_worktree_roots(workspace: Path) -> tuple[Path, ...]:
    """Every root a cleanup/health pass must scan, canonical root first.

    *workspace* is the caller's resolved workspace dir, included so a pass driven
    from an explicit workspace never misses it when config resolution disagrees.
    """
    roots = [canonical_worktree_root(), workspace, *sorted(registered_worktree_roots())]
    return tuple(dict.fromkeys(root.expanduser() for root in roots))


def worktrees_outside_the_canonical_root() -> list[Worktree]:
    """Registered worktrees whose checkout is not under :func:`canonical_worktree_root`.

    The namespace-split signal `t3 doctor` reports: each of these is invisible to
    a pass that scans only the canonical root.
    """
    canonical = canonical_worktree_root().expanduser()
    outside: list[Worktree] = []
    for worktree in Worktree.objects.all():
        path = worktree.worktree_path
        if path and not Path(path).expanduser().is_relative_to(canonical):
            outside.append(worktree)
    return outside


__all__ = [
    "canonical_worktree_root",
    "registered_worktree_roots",
    "resolves_as_git_checkout",
    "scanned_worktree_roots",
    "worktrees_outside_the_canonical_root",
]
