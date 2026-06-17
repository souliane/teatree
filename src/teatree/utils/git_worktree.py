"""Worktree management and the teardown data-loss guards.

The worktree partition of :mod:`teatree.utils.git`. Holds worktree add/remove,
the #706 "absent from all remotes" guard, and the bundle-recovery primitive,
all via the :mod:`teatree.utils.git_run` runners.
"""

from teatree.utils.git_run import check, run_strict
from teatree.utils.run import CommandFailedError, run_checked


def commits_absent_from_all_remotes(repo: str, ref: str) -> list[str]:
    """Return ``ref`` commits not reachable from ANY ``refs/remotes/*`` ref.

    The data-loss guard for worktree teardown (#706). ``ref`` is any revision
    git accepts — a branch name, or the literal ``HEAD`` when probing a worktree
    directory directly (robust to a DB-vs-git branch drift and to detached HEAD).
    Unlike :func:`unsynced_commits` (which compares against ``origin/main`` only
    and therefore flags pushed-but-unmerged branches), ``--not --remotes`` is
    empty whenever the tip's own SHA was pushed anywhere — to its own remote
    tracking ref or to main as a fast-forward / merge commit. It is NOT empty for
    a squash-merge: that rewrites the branch's commits into a NEW SHA on the
    default branch, so the original commit is absent-from-all-remotes by SHA even
    though its WORK is shipped — a patch-id comparison
    (:func:`teatree.core.management.commands._workspace_cleanup.is_squash_merged`)
    is what recognises that case. A non-empty result here means these commits
    exist on NO remote BY SHA: removing the worktree on this signal alone would
    destroy a genuinely-unmerged tip. Returns ``"<sha> <subject>"`` lines (newest
    first).

    **Fails closed.** Uses :func:`run_strict` so a non-zero ``git log`` exit
    (invalid/missing ref, corrupt repo, any git error) raises
    ``CommandFailedError`` rather than yielding an empty list. For a data-loss
    guard, "we couldn't determine whether the commits are pushed" must block
    teardown, not allow it. The legitimate empty case (``git log`` exits 0 with
    no output because the ref genuinely has nothing absent from remotes)
    still returns ``[]`` and allows teardown.
    """
    output = run_strict(repo=repo, args=["log", ref, "--not", "--remotes", "--oneline"])
    return [line for line in output.splitlines() if line.strip()]


def worktree_remove(repo: str = ".", path: str = "") -> bool:
    return check(repo=repo, args=["worktree", "remove", "--force", path])


def worktree_add_at_ref(repo: str, path: str, ref: str) -> bool:
    """Materialise a detached worktree at an explicit ``ref`` (SHA or branch).

    The e2e ladder (#794) provisions each repo at a resolved ref — a recorded
    last-green SHA or ``origin/main`` — not only at a branch HEAD. ``git
    worktree add <path> <ref>`` checks out ``ref`` in a detached HEAD, which
    is exactly what running the e2e against a recorded SHA-set requires.
    """
    return check(repo=repo, args=["worktree", "add", "--detach", path, ref])


def worktree_add(repo: str, path: str, branch: str, *, create_branch: bool = True) -> bool:
    args = ["worktree", "add"]
    if create_branch:
        args.extend(["-b", branch])
    args.append(path)
    if not create_branch:
        args.append(branch)
    try:
        run_checked(["git", "-C", repo, *args])
    except CommandFailedError:
        return False
    return True


def bundle_create(repo: str, bundle_path: str, branch: str) -> None:
    """Write a self-contained ``git bundle`` of ``branch`` to ``bundle_path``.

    A bundle carries every commit reachable from the branch tip and is
    restorable with ``git clone <bundle>`` / ``git fetch <bundle>`` — preferred
    over relocating a worktree directory, which leaves git's worktree admin
    pointing at a stale path. Raises ``CommandFailedError`` on failure (the
    caller must not believe a recovery artifact exists when it does not).
    """
    run_strict(repo=repo, args=["bundle", "create", bundle_path, branch])
