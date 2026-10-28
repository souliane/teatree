"""Registered-checkout and path-containment guards for scratch retention."""

import logging
import os

from teatree.core.models.worktree import Worktree

logger = logging.getLogger(__name__)


def worktree_paths() -> frozenset[str] | None:
    """Registered checkout + repo paths; ``None`` when the read fails."""
    try:
        rows = list(Worktree.objects.values_list("repo_path", "extra"))
    except Exception:
        logger.exception("scratch sweep: could not read registered worktrees")
        return None
    paths: set[str] = set()
    for repo_path, extra in rows:
        checkout = extra.get("worktree_path", "") if isinstance(extra, dict) else ""
        paths.update(str(value) for value in (repo_path, checkout) if value)
    return frozenset(paths)


def covers(paths: frozenset[str], candidate: str, *, nested_only: bool) -> bool:
    """True when *candidate* is one of *paths*, contains one, or (unless *nested_only*) is inside one.

    A held fd names a file INSIDE the candidate, so nesting one way is enough there.
    A worktree relationship matters in both directions: the candidate may be the
    checkout, hold it, or sit inside its root.
    """
    for path in paths:
        if path == candidate or path.startswith(candidate + os.sep):
            return True
        if not nested_only and candidate.startswith(path + os.sep):
            return True
    return False
