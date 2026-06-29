"""Worktree management and the teardown data-loss guards.

The worktree partition of :mod:`teatree.utils.git`. Holds worktree add/remove
and the #706 "absent from all remotes" guard, all via the
:mod:`teatree.utils.git_run` runners.
"""

from pathlib import Path

from teatree.utils.git_run import check, run, run_strict
from teatree.utils.run import CommandFailedError, run_checked

# A git reflog line is "<old-sha> <new-sha> <committer> <ts> <tz>\t<message>";
# fewer than two whitespace fields means there is no <new-sha> to recover.
_REFLOG_MIN_FIELDS = 2


def recovered_head_sha_after_ref_gone(wt_path: str) -> str | None:
    """Return the worktree's last HEAD SHA when its checked-out branch ref is gone.

    A forge post-merge branch deletion leaves a worktree's HEAD a *dangling
    symref*: ``refs/heads/<branch>`` is gone, so ``git rev-parse HEAD`` and every
    ``HEAD@{N}`` reflog walk in the worktree dir exit 128 ("unknown revision").
    The tip SHA survives only in the per-worktree HEAD reflog (``logs/HEAD`` under
    the worktree's gitdir), which git itself keeps but cannot resolve through the
    dangling symref. This reads that reflog's most-recent entry — the
    authoritative record of what HEAD pointed at before the ref vanished — and
    returns the resolved commit SHA.

    Used only on the rc=128 branch of the teardown data-loss probe: a recovered
    SHA lets the caller decide by *containment in a remote* instead of refusing
    blindly. Returns ``None`` when there is nothing safe to recover — the dir is
    gone, no reflog exists, the entry is malformed, or the SHA does not resolve to
    a commit in ``wt_path`` — so the caller keeps its fail-closed refusal.
    """
    if not Path(wt_path).is_dir():
        return None
    git_dir = run(repo=wt_path, args=["rev-parse", "--absolute-git-dir"])
    if not git_dir:
        return None
    head_log = Path(git_dir) / "logs" / "HEAD"
    if not head_log.is_file():
        return None
    try:
        last_entry = head_log.read_text(encoding="utf-8").splitlines()[-1]
    except (OSError, IndexError):
        return None
    # The second whitespace field is the SHA HEAD moved TO (the surviving tip).
    fields = last_entry.split()
    if len(fields) < _REFLOG_MIN_FIELDS:
        return None
    candidate = fields[1]
    resolved = run(repo=wt_path, args=["rev-parse", "--verify", "--quiet", f"{candidate}^{{commit}}"])
    return resolved or None


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


def worktree_move(repo: str, src: str, dst: str) -> None:
    """``git worktree move <src> <dst>`` run from *repo* (the source clone).

    Updates git's worktree admin (the per-worktree gitdir + the gitfile pointer)
    so the moved worktree stays linked to its clone — the reason a raw ``mv`` is
    wrong (it leaves git's metadata pointing at the stale path). Run from *repo*
    (the clone, or any OTHER worktree), never from inside *src*: git refuses to
    move the worktree it is currently sitting in. Raises ``CommandFailedError``
    on failure so the caller can report-and-continue.
    """
    run_strict(repo=repo, args=["worktree", "move", src, dst])


def locked_worktree_paths(repo: str) -> set[str]:
    """Resolved paths of *repo*'s git-locked worktrees (``git worktree list --porcelain``).

    A ``locked`` line in the porcelain listing marks the preceding ``worktree``
    entry as locked; a locked worktree must never be relocated. Paths are
    ``resolve()``-d so they compare equal to a caller's ``Path(...).resolve()``.
    """
    locked: set[str] = set()
    current: str | None = None
    for line in run(repo=repo, args=["worktree", "list", "--porcelain"]).splitlines():
        if line.startswith("worktree "):
            current = line[len("worktree ") :]
        elif line.startswith("locked") and current is not None:
            locked.add(str(Path(current).resolve()))
    return locked


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
