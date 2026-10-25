"""Branch and ref discovery: default branch, merged-state, current branch, HEAD.

The branch/ref-shaped partition of :mod:`teatree.utils.git`. Every function
here resolves or mutates a branch/ref by shelling out through the
:mod:`teatree.utils.git_run` runners.
"""

import os
import re
from dataclasses import dataclass
from enum import Enum
from subprocess import TimeoutExpired

from teatree.utils.git_remote_ops import config_value
from teatree.utils.git_run import check, git_env_non_interactive, run, run_strict, run_with_status

DETACHED_HEAD = "HEAD"

DIFF_BASE_ENV = "T3_DIFF_COVERAGE_BASE"
DIFF_BASE_CONFIG_KEY = "teatree.targetBranch"

_RAW_SHA = re.compile(r"\A[0-9a-f]{7,40}\Z")
REMOTE_REF_TIMEOUT_SECONDS = 30


class RefPresence(Enum):
    PRESENT = "present"
    ABSENT = "absent"
    UNKNOWN = "unknown"


@dataclass(frozen=True, slots=True)
class RemoteRefListing:
    heads: frozenset[str] | None
    timed_out: bool = False


def list_remote_refs(repo: str) -> RemoteRefListing:
    try:
        result = run_with_status(
            repo=repo,
            args=["ls-remote", "--heads", "origin"],
            env=git_env_non_interactive(),
            timeout=REMOTE_REF_TIMEOUT_SECONDS,
        )
    except TimeoutExpired:
        return RemoteRefListing(None, timed_out=True)
    except OSError:
        return RemoteRefListing(None)
    if result.returncode != 0:
        return RemoteRefListing(None)
    names = frozenset(
        line.partition("refs/heads/")[2] for line in result.stdout.splitlines() if "\trefs/heads/" in line
    )
    return RemoteRefListing(names)


def show_ref(repo: str, ref: str) -> RefPresence:
    try:
        result = run_with_status(repo=repo, args=["show-ref", "--verify", "--quiet", ref])
    except OSError:
        return RefPresence.UNKNOWN
    return {0: RefPresence.PRESENT, 1: RefPresence.ABSENT}.get(result.returncode, RefPresence.UNKNOWN)


def _local_ref_presence(repo: str, branch: str) -> RefPresence:
    for ref in (f"refs/heads/{branch}", f"refs/remotes/origin/{branch}"):
        presence = show_ref(repo, ref)
        if presence is not RefPresence.ABSENT:
            return presence
    return RefPresence.ABSENT


def ref_presence(repo: str, branch: str, listing: RemoteRefListing) -> RefPresence:
    local = _local_ref_presence(repo, branch)
    if local is not RefPresence.ABSENT:
        return local
    if listing.heads is None:
        return RefPresence.UNKNOWN
    return RefPresence.PRESENT if branch in listing.heads else RefPresence.ABSENT


def resolve_diff_base(repo: str = ".") -> str:
    """Resolve the ref the per-diff gates diff against (BLUEPRINT §17.6.3).

    A hardcoded ``origin/main`` grades every commit as new whenever the branch's
    real base is NOT ``main`` — a ``master``-default repo, or a fork whose
    integration branch is ahead of a stale ``main`` — so the whole history diffs
    as uncovered. Resolution order:

    1. ``T3_DIFF_COVERAGE_BASE`` — the per-invocation override.
    2. the ``teatree.targetBranch`` git config — the branch every PR in this
        checkout targets. A fork whose work lands on an integration branch
        declares it here, and it is the same key the main-clone commit guard
        reads, so one declaration serves both.
    3. the repo's ACTUAL default branch (``origin/HEAD``), so a ``master``-default
        repo resolves ``origin/master`` rather than a nonexistent ``origin/main``.
    4. ``origin/main`` only as the last-resort fallback (default branch unresolvable).

    A configured value from either of the first two rungs is qualified the same
    way: a bare name becomes ``origin/<name>``; an already-qualified ref
    (``origin/…`` / ``refs/…``) or a raw SHA passes through untouched.
    """
    configured = os.environ.get(DIFF_BASE_ENV, "").strip() or config_value(repo, DIFF_BASE_CONFIG_KEY).strip()
    if configured:
        return _qualify(configured)
    try:
        return f"origin/{default_branch(repo)}"
    except RuntimeError:
        return "origin/main"


def _qualify(ref: str) -> str:
    if ref.startswith(("origin/", "refs/")) or _RAW_SHA.match(ref):
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
