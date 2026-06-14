"""Remote-sync operations: fetch, rebase, merge, pull, push.

The sync partition of :mod:`teatree.utils.git`. Every function moves the local
ref relative to a remote (or merges/rebases onto a target), all via the
:mod:`teatree.utils.git_run` runners.
"""

from teatree.utils.git_run import check, run, run_strict


def fetch(repo: str = ".", remote: str = "origin", ref: str = "") -> None:
    args = ["fetch", remote]
    if ref:
        args.append(ref)
    run(repo=repo, args=args)


def rebase(repo: str = ".", target: str = "") -> None:
    run_strict(repo=repo, args=["rebase", target])


def merge_no_edit(repo: str = ".", target: str = "") -> bool:
    """``git merge --no-edit <target>`` — returns ``True`` on success.

    The branch-currency gate's primitive (#940). Fast-forward is
    preferred (the default ``git merge`` posture); a non-FF merge with
    an empty editor commit is created when fast-forward isn't possible.
    A conflict yields ``False`` — the caller is expected to call
    :func:`merge_abort` to restore the worktree.
    """
    return check(repo=repo, args=["merge", "--no-edit", target])


def merge_abort(repo: str = ".") -> None:
    """``git merge --abort`` — best-effort restore of the pre-merge tree.

    A no-op when no merge is in progress (the command exits non-zero
    but does no harm), so safe to call unconditionally as part of the
    branch-currency gate's conflict-cleanup path.
    """
    check(repo=repo, args=["merge", "--abort"])


def pull_ff_only(repo: str = ".") -> bool:
    return check(repo=repo, args=["pull", "--ff-only"])


def push(repo: str = ".", remote: str = "origin", branch: str = "") -> None:
    args = ["push", "--set-upstream", remote]
    if branch:
        args.append(branch)
    run_strict(repo=repo, args=args)
