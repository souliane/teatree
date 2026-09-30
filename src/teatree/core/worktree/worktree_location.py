"""Where a ``Worktree`` row's checkout lives on disk."""

from pathlib import Path
from typing import TYPE_CHECKING

from teatree.core.worktree.worktree_paths import worktree_dir_for

if TYPE_CHECKING:
    from teatree.core.models import Worktree


def resolve_worktree_path(workspace: Path, worktree: "Worktree") -> str:
    """Return the on-disk worktree path, preferring extras and falling back to the canonical layout.

    Provisioning records ``worktree_path`` in ``Worktree.extra`` after a
    successful ``git worktree add``. When that record is missing (extras lost,
    row created before the path was set, manual provisioning), fall back to the
    shared :func:`worktree_dir_for` ``<workspace>/<branch>/<repo-leaf>`` layout.
    """
    stored = (worktree.extra or {}).get("worktree_path", "")
    if stored:
        return stored
    return str(worktree_dir_for(workspace, worktree.branch, worktree.repo_path))
