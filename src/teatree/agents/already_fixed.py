"""A coding result that lands no commit because the branch already holds the fix."""

from collections.abc import Iterable, Mapping
from pathlib import Path

from teatree.core.models.fix_record_types import already_fixed_claim
from teatree.core.models.worktree import Worktree
from teatree.utils import git
from teatree.utils.run import CommandFailedError


def already_fixed_accepted(result: Mapping[str, object] | None, worktrees: Iterable[Worktree]) -> bool:
    """Whether the claim names non-blank evidence and the HEAD one of *worktrees* is checked out at."""
    verified_head, evidence = already_fixed_claim((result or {}).get("fix_record"))
    if not verified_head or not evidence:
        return False
    return any(_head_of(worktree) == verified_head for worktree in worktrees)


def _head_of(worktree: Worktree) -> str:
    extra = worktree.extra if isinstance(worktree.extra, dict) else {}
    path = extra.get("worktree_path") or worktree.repo_path
    if not path or not Path(path).is_dir():
        return ""
    try:
        return git.head_sha(repo=str(path)).lower()
    except (CommandFailedError, OSError):
        return ""
