"""Restore the caller's checked-out branch around the keystone merge (#2383).

The §17.4 keystone runs from inside the primary clone (the orchestrator/loop
drives ``ticket clear`` + ``ticket merge`` there, then ff-syncs ``main``). The
merge transport is API-only, but the cross-repo SHA-recovery probe
(:func:`pr_slug_resolution._reconcile_slug_against_reviewed_sha`) and any future
fallback that has to read a PR's tree LOCALLY can ``git checkout`` a branch in
the cwd repo. Left unrestored, that detaches the clone's HEAD at the merged PR
branch tip, and the next ``git pull --ff-only origin/main`` aborts with "Not
possible to fast-forward" — the #2383 incident.

This guard makes the invariant structural rather than per-call-site vigilance:
:func:`restore_caller_branch` captures the clone's checked-out ref BEFORE the
merge and restores it AFTER, even on error. Whatever the merge path does to HEAD
in between — today nothing, tomorrow a local probe checkout — the caller's
checkout is exactly what it was. Best-effort and crash-proof: a capture or
restore failure never masks the merge result or a merge exception.
"""

import logging
from collections.abc import Iterator
from contextlib import contextmanager

from teatree.utils.run import run_allowed_to_fail

logger = logging.getLogger(__name__)


def _git(repo: str, *args: str) -> tuple[int, str]:
    result = run_allowed_to_fail(["git", "-C", repo, *args], expected_codes=None)
    return result.returncode, result.stdout.strip()


def _capture_head(repo: str) -> tuple[str, str]:
    """The clone's current checkout as ``(branch, detached_sha)``.

    On a branch: ``(branch_name, "")``. Detached: ``("", head_sha)``. When git
    cannot answer (not a repo, no HEAD yet, any error): ``("", "")`` — the guard
    then has nothing to restore and stays a no-op.
    """
    branch_rc, branch = _git(repo, "symbolic-ref", "--quiet", "--short", "HEAD")
    if branch_rc == 0 and branch:
        return branch, ""
    sha_rc, sha = _git(repo, "rev-parse", "HEAD")
    if sha_rc == 0 and sha:
        return "", sha
    return "", ""


def _restore_head(repo: str, branch: str, detached_sha: str) -> None:
    """Move HEAD back to the captured ref iff it drifted off it."""
    target = branch or detached_sha
    if not target:
        return
    _, current_branch = _git(repo, "symbolic-ref", "--quiet", "--short", "HEAD")
    _, current_sha = _git(repo, "rev-parse", "HEAD")
    already_restored = current_branch == branch if branch else (not current_branch and current_sha == detached_sha)
    if already_restored:
        return
    rc, _ = _git(repo, "checkout", target)
    if rc != 0:
        logger.warning(
            "merge head_guard: could not restore %s in %s after the keystone merge — "
            "the clone may be left on the merged PR branch; recover with `git checkout %s`",
            target,
            repo,
            target,
        )


@contextmanager
def restore_caller_branch(repo: str | None) -> Iterator[None]:
    """Restore *repo*'s checked-out ref after the keystone merge (#2383).

    A no-op when *repo* is ``None`` (no project root resolved) or git cannot
    report a HEAD to capture. The restore runs in ``finally`` so it fires even
    when the merge raises — a refused merge that probed a branch locally must
    still leave the caller's checkout untouched.
    """
    if repo is None:
        yield
        return
    branch, detached_sha = _capture_head(repo)
    try:
        yield
    finally:
        try:
            _restore_head(repo, branch, detached_sha)
        except Exception:
            logger.exception("merge head_guard: restore raised in %s; merge result preserved", repo)
