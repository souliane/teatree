"""Recoverable capture of dirty/unpushed worktrees before ``clean-all`` prunes them (#835).

``clean-all`` prunes worktrees as part of cleanup. When it force-removes a
worktree that still has uncommitted changes or unpushed commits, that work is
destroyed irreversibly — this actually happened (a concurrent ``clean-all``
reaped a completed-but-uncommitted change set, costing a full reimplementation).

Refusing to prune dirty worktrees only trades data loss for a stuck-cleanup
state. The better fix is to make the destructive step **recoverable**: before
removing such a worktree, write a self-contained, restorable artifact under the
system temp dir, then proceed with removal.

A ``git bundle`` of the branch (all local commits) is preferred over relocating
the worktree directory: a moved worktree leaves git's worktree admin pointing at
a stale path, whereas a bundle is self-contained and trivially restorable
(``git clone`` / ``git fetch`` from the bundle). The uncommitted working-tree
changes — staged, unstaged, and untracked — are captured alongside as a single
``git diff`` patch applicable with ``git apply``.

**Out of scope by design:** no TTL, quota, or purge logic. The artifacts live in
the OS temp directory and are reaped by the OS's own policy (macOS: the daily
periodic job removes temp files untouched for ~3 days; Linux: ``systemd-tmpfiles``,
distro-dependent). A bundle+diff is tiny, so quota is a non-issue and the
recovery window is ample given that work is normally committed well within that
period. This keeps the implementation minimal and lets the OS manage its own
temp lifecycle.
"""

import logging
import shutil
import tempfile
from datetime import UTC, datetime
from pathlib import Path

from teatree.core.models import Worktree
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


def capture_recovery_artifact(repo_main: Path, wt_path: str, worktree: Worktree) -> Path | None:
    """Capture a restorable artifact for a dirty/unpushed worktree, or do nothing.

    Returns the recovery directory when an artifact was written (the worktree
    had uncommitted changes and/or unpushed commits), or ``None`` when there was
    nothing to lose (clean working tree whose branch is fully pushed) — the
    clean+merged hard-delete path is unchanged.

    The artifact directory ``<tempdir>/t3-recover-<id>-<UTC timestamp>/``
    contains two files. ``branch.bundle`` is a ``git bundle create`` of the
    whole branch, restorable via ``git clone <bundle>`` / ``git fetch
    <bundle>``. ``working-tree.diff`` is a single ``git diff`` patch covering
    staged, unstaged, and untracked changes, restorable via ``git apply``.
    """
    wt = Path(wt_path)
    if not wt.is_dir():
        return None

    dirty = bool(git.status_porcelain(str(wt)))
    unpushed = _has_unpushed_commits(repo_main, worktree.branch)
    if not dirty and not unpushed:
        return None

    label = worktree.ticket.ticket_number
    safe_label = "".join(c if c.isalnum() or c in "-_" else "-" for c in label)
    timestamp = datetime.now(tz=UTC).strftime("%Y%m%dT%H%M%SZ")
    prefix = f"t3-recover-{safe_label}-{timestamp}-"
    recovery_dir = Path(tempfile.mkdtemp(prefix=prefix, dir=tempfile.gettempdir()))

    try:
        bundle_path = recovery_dir / _BUNDLE_NAME
        git.bundle_create(str(repo_main), str(bundle_path), worktree.branch)

        diff_path = recovery_dir / _DIFF_NAME
        diff_path.write_text(git.full_worktree_diff(str(wt)), encoding="utf-8")
    except Exception:
        # A half-written artifact is worse than none and a stray empty temp dir
        # is litter — drop our own partial dir, then re-raise so the caller logs
        # and surfaces the failure (it still proceeds with the prune; #835
        # rejects blocking cleanup on capture trouble).
        shutil.rmtree(recovery_dir, ignore_errors=True)
        raise

    logger.warning(
        "%s (%s): dirty/unpushed worktree — wrote recovery artifact to %s before prune",
        worktree.repo_path,
        worktree.branch,
        recovery_dir,
    )
    return recovery_dir
