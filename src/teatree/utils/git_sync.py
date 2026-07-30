"""Remote-sync operations: fetch, rebase, merge, pull, push.

The sync partition of :mod:`teatree.utils.git`. Every function moves the local
ref relative to a remote (or merges/rebases onto a target), all via the
:mod:`teatree.utils.git_run` runners.
"""

import subprocess

from teatree.utils.git_run import check, git_env_without_overrides, run, run_strict
from teatree.utils.run import run_allowed_to_fail

FETCH_PRUNE_TIMEOUT_SECONDS = 120.0


def fetch(repo: str = ".", remote: str = "origin", ref: str = "") -> None:
    args = ["fetch", remote]
    if ref:
        args.append(ref)
    run(repo=repo, args=args)


def fetch_all_prune(repo: str = ".") -> bool:
    """``git fetch --all --prune`` — the freshness precondition for the #706 data-loss gate.

    :func:`teatree.utils.git_worktree.commits_absent_from_all_remotes` answers
    "is this commit on a remote?" by a purely LOCAL graph query over
    ``refs/remotes/*``. Those refs go STALE the moment a branch is deleted
    upstream by anything other than this clone — the ordinary post-merge case
    (a forge's auto-delete-on-merge, or a sibling clone). A stale tracking ref
    makes an unpushed tip look reachable-from-a-remote, so a destructive caller
    reads genuinely-unmerged work as "already pushed" and reaps the last copy.
    Pruning first removes exactly that false evidence.

    ``--all`` because the gate's contract is "absent from ALL remotes", so every
    remote must be refreshed, not just ``origin``.

    **Every destructive caller MUST fail closed on ``False``** — keep the
    worktree/branch, delete nothing — because a failed refresh means remote
    state is unknown, and "unknown" must never authorise a deletion. Returns
    ``True`` only on a clean exit; a non-zero exit or a timeout yields ``False``.
    ``GIT_TERMINAL_PROMPT=0`` ensures a remote demanding interactive credentials
    fails fast instead of hanging the sweep on a password prompt.
    """
    env = git_env_without_overrides() | {"GIT_TERMINAL_PROMPT": "0"}
    try:
        result = run_allowed_to_fail(
            ["git", "-C", repo, "fetch", "--all", "--prune", "--quiet"],
            expected_codes=None,
            env=env,
            timeout=FETCH_PRUNE_TIMEOUT_SECONDS,
        )
    except subprocess.TimeoutExpired:
        return False
    return result.returncode == 0


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
