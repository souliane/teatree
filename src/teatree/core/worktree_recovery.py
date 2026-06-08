"""Recoverable capture of dirty/unpushed worktrees before ``clean-all`` prunes them (#835).

``clean-all`` prunes worktrees as part of cleanup. When it force-removes a
worktree that still has uncommitted changes or unpushed commits, that work is
destroyed irreversibly — this actually happened (a concurrent ``clean-all``
reaped a completed-but-uncommitted change set, costing a full reimplementation).

Refusing to prune dirty worktrees only trades data loss for a stuck-cleanup
state. The better fix is to make the destructive step **recoverable**: before
removing such a worktree, write a self-contained, restorable artifact under the
system temp dir, then proceed with removal.

The capture mechanics (``git bundle`` of the branch + a single ``git diff``
patch of staged/unstaged/untracked changes) live in the Django-free
:mod:`teatree.core.worktree_snapshot` so the ``SubagentStop`` hook (#1764),
which runs under a bare ``python3``, shares the exact same primitive. This
module is the ORM-aware adapter: it extracts ``branch`` and the ticket label
from a :class:`~teatree.core.models.Worktree` row and delegates.

**Out of scope by design:** no TTL, quota, or purge logic. The artifacts live in
the OS temp directory and are reaped by the OS's own policy (macOS: the daily
periodic job removes temp files untouched for ~3 days; Linux: ``systemd-tmpfiles``,
distro-dependent). A bundle+diff is tiny, so quota is a non-issue and the
recovery window is ample given that work is normally committed well within that
period. This keeps the implementation minimal and lets the OS manage its own
temp lifecycle.
"""

from pathlib import Path

from teatree.core.models import Worktree
from teatree.core.worktree_snapshot import _has_unpushed_commits, capture_worktree_snapshot
from teatree.utils import git

__all__ = ["_has_unpushed_commits", "capture_recovery_artifact"]


def capture_recovery_artifact(
    repo_main: Path,
    wt_path: str,
    worktree: Worktree,
    *,
    branch: str | None = None,
) -> Path | None:
    """Capture a restorable artifact for a dirty/unpushed worktree, or do nothing.

    ORM-aware adapter over :func:`teatree.core.worktree_snapshot.capture_worktree_snapshot`:
    resolves the branch and the ticket label from the :class:`Worktree` row, then
    delegates the capture. Returns the recovery directory when an artifact was
    written, or ``None`` when there was nothing to lose (clean working tree whose
    branch is fully pushed) — the clean+merged hard-delete path is unchanged.

    ``branch`` overrides the bundled branch with the worktree's EFFECTIVE branch
    when the teardown seam has resolved one from git (it can drift from the DB
    ``Worktree.branch`` slug). When omitted, the DB slug is used. A
    ``DETACHED_HEAD`` override means there is no named branch to bundle, so the
    DB slug is used as the best-effort handle for the branch bundle.
    """
    effective_branch = worktree.branch if branch in {None, git.DETACHED_HEAD} else branch
    return capture_worktree_snapshot(
        repo_main,
        wt_path,
        branch=effective_branch,
        label=worktree.ticket.ticket_number,
    )
