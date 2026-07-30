"""Branch and ref discovery: default branch, merged-state, current branch, HEAD.

The branch/ref-shaped partition of :mod:`teatree.utils.git`. Every function
here resolves or mutates a branch/ref by shelling out through the
:mod:`teatree.utils.git_run` runners.
"""

import os

from teatree.utils.git_run import check, run, run_strict

DETACHED_HEAD = "HEAD"

DIFF_BASE_ENV = "T3_DIFF_COVERAGE_BASE"


def resolve_diff_base(repo: str = ".") -> str:
    """Resolve the ref the per-diff gates diff against (BLUEPRINT §17.6.3).

    A hardcoded ``origin/main`` grades every commit as new whenever the branch's
    real base is NOT ``main`` — a ``master``-default repo, or a fork whose
    integration branch is ahead of a stale ``main`` — so the whole history diffs
    as uncovered. Resolution order:

    1. ``T3_DIFF_COVERAGE_BASE`` — the *configured* base branch. A bare name is
        remote-qualified (``origin/<name>``); an already-qualified ref
        (``origin/…`` / ``refs/…``) or a SHA passes through untouched.
    2. the repo's ACTUAL default branch (``origin/HEAD``), so a ``master``-default
        repo resolves ``origin/master`` rather than a nonexistent ``origin/main``.
    3. ``origin/main`` only as the last-resort fallback (default branch unresolvable).
    """
    configured = os.environ.get(DIFF_BASE_ENV, "").strip()
    if configured:
        return _qualify(configured)
    try:
        return f"origin/{default_branch(repo)}"
    except RuntimeError:
        return "origin/main"


def _qualify(ref: str) -> str:
    if ref.startswith(("origin/", "refs/")):
        return ref
    return f"origin/{ref}"


def default_branch(repo: str = ".") -> str:
    ref = run(repo=repo, args=["symbolic-ref", "refs/remotes/origin/HEAD"])
    branch = ref.replace("refs/remotes/origin/", "")
    if branch:
        return branch

    for candidate in ("main", "master", "development"):
        if check(repo=repo, args=["show-ref", "--verify", "--quiet", f"refs/remotes/origin/{candidate}"]):
            return candidate

    msg = f"Could not detect default branch for {repo}"
    raise RuntimeError(msg)


def branch_merged(repo: str, branch: str, target: str = "origin/main") -> bool:
    output = run(repo=repo, args=["branch", "--merged", target])
    return any(line.strip() == branch for line in output.splitlines())


def current_branch(repo: str = ".") -> str:
    """Return the branch checked out in ``repo``, or ``DETACHED_HEAD`` when detached.

    ``rev-parse --abbrev-ref HEAD`` resolves the symbolic branch name on a
    branch and the literal string ``HEAD`` (``DETACHED_HEAD``) when the worktree
    is in detached HEAD. The teardown seam uses this to resolve a worktree's
    EFFECTIVE branch from git rather than trusting a possibly-drifted DB
    ``Worktree.branch`` row.
    """
    return run(repo=repo, args=["rev-parse", "--abbrev-ref", "HEAD"])


def head_sha(repo: str = ".") -> str:
    """Return the full 40-char SHA of ``HEAD`` (the code-under-test SHA).

    Used by the e2e work-item provenance recorder (#794) so a run records
    the *exact* commit it tested, not a branch name that drifts.
    """
    return run_strict(repo=repo, args=["rev-parse", "HEAD"])


def branch_delete(repo: str = ".", branch: str = "") -> bool:
    return check(repo=repo, args=["branch", "-D", branch])
