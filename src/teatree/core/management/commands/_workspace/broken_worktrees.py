"""Reap BROKEN worktree checkouts that no longer resolve as git repos (#3583).

A worktree dir whose ``.git`` pointer no longer resolves — its admin entry was
pruned from the source clone, or the clone itself was removed — is a dead
checkout: ``git rev-parse`` fails inside it, so no git operation can reach its
contents and no reaper keyed on ``git worktree list`` can see it either. They
accumulate silently (a real host carried 16, each emitting a setup-time
``is not a git checkout`` WARN on every session) and cost agents time reasoning
about whether a stale sibling is live.

The pass is deliberately narrow. Only an immediate child dir of a worktree root
that CARRIES a ``.git`` entry is a candidate — a dir with no ``.git`` was never a
checkout (an auto-isolated env dir, a scratch dir) and belongs to another reaper
or to nobody. Of those candidates, one that still resolves is a healthy worktree
and is left to the row-driven and raw-orphan reapers; one that does NOT resolve
is broken by definition and is removed, since a broken checkout can hold no
recoverable git work.

The safety guards mirror the #706/#835 data-loss discipline: a dir a live
``Worktree`` row still points at is never touched here (its row owns its
lifecycle), and a ``clean_ignore`` match is always skipped.
"""

import logging
import shutil
from pathlib import Path

from teatree.core.cleanup.clean_ignore import is_clean_ignored
from teatree.core.models import Worktree
from teatree.core.worktree.worktree_paths import paths_match
from teatree.core.worktree.worktree_roots import resolves_as_git_checkout

logger = logging.getLogger(__name__)


def _db_tracked_paths() -> list[str]:
    return [wt.worktree_path for wt in Worktree.objects.all() if wt.worktree_path]


def _candidate_dirs(root: Path) -> list[Path]:
    """Immediate child dirs of *root* that carry a ``.git`` entry (former checkouts)."""
    if not root.is_dir():
        return []
    return sorted(child for child in root.iterdir() if child.is_dir() and (child / ".git").exists())


def reap_broken_worktree_dirs(*roots: Path) -> list[str]:
    """Remove dead worktree checkouts under each root in *roots*; report every decision.

    A root is scanned only for its immediate children. Passing several roots is
    how the alternate-root split is drained: an operator who accumulated
    worktrees outside the canonical :func:`teatree.config.worktree_root` hands
    both roots in and ends up with one location, since only the canonical root is
    ever written to afterwards.
    """
    tracked = _db_tracked_paths()
    outcomes: list[str] = []
    seen: set[Path] = set()
    for root in roots:
        for candidate in _candidate_dirs(root):
            if candidate in seen:
                continue
            seen.add(candidate)
            outcomes.extend(_reap_one(candidate, tracked=tracked))
    return outcomes


def _reap_one(candidate: Path, *, tracked: list[str]) -> list[str]:
    if resolves_as_git_checkout(candidate):
        return []
    label = candidate.name
    if is_clean_ignored(label):
        return [f"SKIPPED broken worktree '{label}': matches clean_ignore — keeping"]
    if any(paths_match(str(candidate), path) for path in tracked):
        return [f"SKIPPED broken worktree '{label}': a Worktree row still tracks it — its row owns the teardown"]
    try:
        shutil.rmtree(candidate)
    except OSError as exc:
        logger.warning("Could not remove broken worktree dir %s: %s", candidate, exc)
        return [f"SKIPPED broken worktree '{label}': removal failed ({exc})"]
    return [f"Removed broken worktree (git rev-parse fails, no recoverable work): {label}"]


__all__ = ["reap_broken_worktree_dirs"]
