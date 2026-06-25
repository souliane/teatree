"""Django-free capture of a dirty/unpushed worktree as a restorable artifact (#1764).

The single source of truth for "bundle the branch + diff the working tree into a
restorable temp artifact". :func:`teatree.core.worktree_recovery.capture_recovery_artifact`
(the ``clean-all`` prune path, #835) delegates here, and the ``SubagentStop``
hook calls it directly: that hook runs under a bare ``python3`` with no Django
configured, so the capture primitive must depend only on
:mod:`teatree.utils.git` / :mod:`teatree.utils.run`, never on a ``Worktree`` ORM
row.

A ``git bundle`` of the branch (all local commits) plus a single ``git diff``
patch (staged, unstaged, and untracked changes) lands under the OS temp dir,
restorable via ``git clone`` / ``git fetch`` + ``git apply``. No TTL/quota
logic — the OS reaps temp files by its own policy (#835).
"""

import logging
import shutil
import tempfile
from datetime import UTC, datetime
from pathlib import Path

from teatree.utils import git
from teatree.utils.run import CommandFailedError

logger = logging.getLogger(__name__)

_BUNDLE_NAME = "branch.bundle"
_DIFF_NAME = "working-tree.diff"


def _has_unpushed_commits(repo_main: Path, branch: str) -> bool:
    """Return whether ``branch`` has commits absent from every remote ref.

    Fails *open* for capture: an inconclusive probe (corrupt repo, missing
    branch) is treated as "might have unpushed work", so we still capture rather
    than risk silently dropping commits we could not prove are pushed.
    """
    try:
        return bool(git.commits_absent_from_all_remotes(str(repo_main), branch))
    except CommandFailedError:
        return True


_RECOVERY_BRANCH = "t3-recovered-detached"


def _recovered_head_sha_if_in_remote(wt: Path) -> str | None:
    """The recovered HEAD SHA of a branch-ref-gone worktree, only if it is in a remote.

    A forge post-merge branch deletion leaves the worktree's HEAD a dangling
    symref, so a real ``git rev-parse HEAD`` fails. This recovers the last HEAD
    SHA from the per-worktree reflog and returns it ONLY when that SHA is
    POSITIVELY contained in a remote — the case where the committed tip is
    genuinely safe on a remote. Returns ``None`` when HEAD resolves normally
    (not a dangling symref), the HEAD is unrecoverable, the containment probe
    errors, or the tip is on no remote — fails *closed* so the normal capture
    path still runs (it fails open to capturing).
    """
    if git.check(repo=str(wt), args=["rev-parse", "--verify", "--quiet", "HEAD"]):
        return None
    sha = git.recovered_head_sha_after_ref_gone(str(wt))
    if not sha:
        return None
    try:
        return sha if not git.commits_absent_from_all_remotes(str(wt), sha) else None
    except CommandFailedError:
        return None


def capture_worktree_snapshot(repo_main: Path, wt_path: str, *, branch: str, label: str) -> Path | None:
    """Capture a restorable artifact for a dirty/unpushed worktree, or do nothing.

    Returns the recovery directory when an artifact was written (the worktree
    had uncommitted changes and/or unpushed commits on ``branch``), or ``None``
    when there was nothing to lose (clean working tree whose branch is fully
    pushed). The directory ``<tempdir>/t3-recover-<label>-<UTC timestamp>/``
    holds ``branch.bundle`` (``git clone`` / ``git fetch`` restorable) and
    ``working-tree.diff`` (``git apply`` restorable). A partial artifact is
    cleaned up and the error re-raised so a caller never believes a snapshot
    exists when it does not.

    ``branch`` may be a named branch or the literal ``HEAD`` (``git.DETACHED_HEAD``)
    when the worktree is in detached HEAD. A named branch is meaningful in the
    shared object store, so its probe + bundle run from ``repo_main``. ``HEAD``
    is meaningful only in the worktree dir (it resolves to the detached commit
    there, but to the main clone's tip in ``repo_main``), so its probe + bundle
    run from ``wt_path`` itself — otherwise the bundle would capture the wrong
    commits or refuse as empty, and the detached commits would be lost.
    """
    wt = Path(wt_path)
    if not wt.is_dir():
        return None

    if branch == git.DETACHED_HEAD:
        recovered_sha = _recovered_head_sha_if_in_remote(wt)
        if recovered_sha is not None:
            # The worktree's branch ref was deleted post-merge → HEAD is a
            # dangling symref, and the committed tip is safe on a remote. The
            # tip being in a remote does NOT make an UNCOMMITTED delta
            # recoverable, so "is there work to capture?" cannot be decided from
            # ``git status --porcelain`` shape: with no resolvable HEAD git
            # collapses the HEAD-vs-index column to ``A`` for EVERY indexed path,
            # so a staged content-modify or a staged rename looks identical to an
            # unmodified tracked file (the same bare ``"A "``). The robust signal
            # is the SAME diff the capture writes: the worktree diffed against the
            # recovered tip — non-empty for edit / staged-modify / rename /
            # untracked alike (``full_worktree_diff`` marks untracked intent-to-add
            # so they appear), empty only when the tree genuinely matches the tip.
            # Empty → clean no-op (caller reaps the orphan). Non-empty → capture,
            # reusing the exact diff so detection can never diverge from what the
            # artifact preserves (the dangling ``HEAD`` itself cannot be diffed or
            # bundled, rc=128). #706/#835/#1506 capture-or-refuse: never silent-destroy.
            recovery_diff = git.full_worktree_diff(str(wt), base=recovered_sha)
            if not recovery_diff:
                return None
            return _capture_dangling_head_dirty(repo_main, wt, recovered_sha, recovery_diff, label)

    bundle_repo = wt if branch == git.DETACHED_HEAD else repo_main

    dirty = bool(git.status_porcelain(str(wt)))
    unpushed = _has_unpushed_commits(bundle_repo, branch)
    if not dirty and not unpushed:
        return None

    recovery_dir = _new_recovery_dir(label)
    try:
        git.bundle_create(str(bundle_repo), str(recovery_dir / _BUNDLE_NAME), branch)
        (recovery_dir / _DIFF_NAME).write_text(git.full_worktree_diff(str(wt)), encoding="utf-8")
    except Exception:
        shutil.rmtree(recovery_dir, ignore_errors=True)
        raise

    logger.warning("%s (%s): dirty/unpushed worktree — wrote recovery artifact to %s", repo_main, branch, recovery_dir)
    return recovery_dir


def _new_recovery_dir(label: str) -> Path:
    safe_label = "".join(c if c.isalnum() or c in "-_" else "-" for c in (label or "unknown"))
    timestamp = datetime.now(tz=UTC).strftime("%Y%m%dT%H%M%SZ")
    prefix = f"t3-recover-{safe_label}-{timestamp}-"
    return Path(tempfile.mkdtemp(prefix=prefix, dir=tempfile.gettempdir()))


def _capture_dangling_head_dirty(repo_main: Path, wt: Path, recovered_sha: str, recovery_diff: str, label: str) -> Path:
    """Capture a dirty dangling-HEAD worktree against its recovered tip SHA.

    ``git rev-parse HEAD`` exits 128 in a dangling-HEAD worktree, so a
    ``bundle_create`` of ``HEAD`` cannot run; the bundle is re-anchored on
    ``recovered_sha`` (the surviving tip recovered from the per-worktree reflog,
    proven to be in a remote) under a transient ``refs/heads/`` recovery branch
    so a ``git clone`` checks it out. ``recovery_diff`` is the
    already-computed ``full_worktree_diff(wt, base=recovered_sha)`` the caller
    used to decide this worktree is dirty — reused verbatim as the captured
    patch so detection and the artifact can never diverge. A partial artifact is
    cleaned up and the error re-raised.
    """
    recovery_dir = _new_recovery_dir(label)
    # A unique recovery-branch name per capture so concurrent reaps of two
    # dangling-HEAD worktrees never collide on the shared ``refs/heads/`` store.
    recovery_branch = f"{_RECOVERY_BRANCH}-{recovery_dir.name}"
    try:
        git.bundle_create_at_sha(str(wt), str(recovery_dir / _BUNDLE_NAME), recovered_sha, recovery_branch)
        (recovery_dir / _DIFF_NAME).write_text(recovery_diff, encoding="utf-8")
    except Exception:
        shutil.rmtree(recovery_dir, ignore_errors=True)
        raise

    logger.warning(
        "%s (dangling HEAD, tip %s): dirty worktree — wrote recovery artifact to %s",
        repo_main,
        recovered_sha,
        recovery_dir,
    )
    return recovery_dir
