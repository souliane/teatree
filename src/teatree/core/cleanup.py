"""Shared worktree cleanup logic used by sync (auto-clean on merge) and workspace commands."""

import logging
from contextlib import suppress
from pathlib import Path

from teatree.config import load_config
from teatree.core.models import Ticket, Worktree
from teatree.core.overlay_loader import get_overlay
from teatree.utils import git
from teatree.utils.db import drop_db

logger = logging.getLogger(__name__)


def cleanup_worktree(worktree: Worktree, *, force: bool = False) -> str:
    """Remove a single worktree: git worktree, branch, DB, overlay cleanup.

    Deletes the Worktree record from the database and returns a summary label.
    Errors in individual cleanup steps are suppressed so that partial cleanup
    still succeeds.

    Raises ``RuntimeError`` when *force* is ``False`` and the branch has local
    commits unreachable from any remote (unsynced work that would be lost).
    Pass ``force=True`` only from trusted callers (e.g. tests, programmatic API).
    """
    workspace = load_config().user.workspace_dir
    wt_path = (worktree.extra or {}).get("worktree_path", "")
    overlay = get_overlay()

    if wt_path and Path(wt_path).is_dir() and git.status_porcelain(wt_path):
        logger.warning("%s has uncommitted changes — cleaning anyway (PR merged)", worktree.repo_path)

    for step in overlay.get_cleanup_steps(worktree):
        with suppress(Exception):
            step.callable()

    if wt_path:
        repo_main = workspace / worktree.repo_path
        if repo_main.is_dir():
            if not force:
                unsynced = git.unsynced_commits(str(repo_main), worktree.branch)
                if unsynced:
                    msg = (
                        f"{worktree.repo_path} ({worktree.branch}): "
                        f"refused cleanup — {len(unsynced)} unsynced commit(s). "
                        "Push them to a new branch or pass force=True."
                    )
                    raise RuntimeError(msg)
            git.worktree_remove(str(repo_main), wt_path)
            git.branch_delete(str(repo_main), worktree.branch)

    if worktree.db_name:
        drop_db(worktree.db_name)

    label = f"Cleaned: {worktree.repo_path} ({worktree.branch})"
    ticket_id = worktree.ticket.pk
    worktree.delete()
    if not Worktree.objects.filter(ticket_id=ticket_id).exists():
        Ticket.objects.get(pk=ticket_id).release_redis_slot()
    return label
