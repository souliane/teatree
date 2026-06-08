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

    bundle_repo = wt if branch == git.DETACHED_HEAD else repo_main

    dirty = bool(git.status_porcelain(str(wt)))
    unpushed = _has_unpushed_commits(bundle_repo, branch)
    if not dirty and not unpushed:
        return None

    safe_label = "".join(c if c.isalnum() or c in "-_" else "-" for c in (label or "unknown"))
    timestamp = datetime.now(tz=UTC).strftime("%Y%m%dT%H%M%SZ")
    prefix = f"t3-recover-{safe_label}-{timestamp}-"
    recovery_dir = Path(tempfile.mkdtemp(prefix=prefix, dir=tempfile.gettempdir()))

    try:
        git.bundle_create(str(bundle_repo), str(recovery_dir / _BUNDLE_NAME), branch)
        (recovery_dir / _DIFF_NAME).write_text(git.full_worktree_diff(str(wt)), encoding="utf-8")
    except Exception:
        shutil.rmtree(recovery_dir, ignore_errors=True)
        raise

    logger.warning("%s (%s): dirty/unpushed worktree — wrote recovery artifact to %s", repo_main, branch, recovery_dir)
    return recovery_dir
