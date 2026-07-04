"""Real-git managed-main-clone + linked-worktree builders for the Lane-B gate tests.

A *managed main clone* is a primary (``.git`` *dir*) clone whose ``origin`` remote
is ``souliane/teatree``, so ``slug_for_cwd`` resolves it as teatree-managed
offline — the exact shape the main-clone gate protects. A *linked worktree*
(``.git`` *file*) branched off it is where routine git ops are allowed. Both lanes
key their main-clone verdict off this shape, so the tests build the real thing
rather than mock the resolution.
"""

from pathlib import Path

from tests._git_repo import make_git_repo, run_git

MANAGED_REMOTE = "git@github.com:souliane/teatree.git"


def managed_main_clone(path: Path, *, default_branch: str = "main") -> Path:
    """A primary clone (``.git`` dir) with the ``souliane/teatree`` origin + one commit."""
    make_git_repo(path, default_branch=default_branch)
    run_git(path, "remote", "add", "origin", MANAGED_REMOTE)
    return path


def linked_worktree(clone: Path, wt: Path, *, branch: str = "feat-x") -> Path:
    """A linked worktree (``.git`` file) branched off *clone*."""
    run_git(clone, "worktree", "add", "-b", branch, str(wt))
    return wt
