"""Reap orphaned RAW git worktrees that no teatree ``Worktree`` row tracks (#2361).

Sub-agents and coders create their own worktrees with a bare ``git worktree
add`` — no ``Worktree`` DB row. ``clean-all``'s row-driven reaper never iterates
them, so they accumulate indefinitely (a real host reached 183 of them). This
module is the gap-closing pass: discover every git worktree under the
workspace's main clones, subtract the DB-tracked set, and dispose of the
remainder under a safety-first policy.

Disposition per orphan (the #706 data-loss contract is never bypassed). A
*merged / gone / no-unique-work* orphan — whose commits are already on
``origin/<default>``, or a detached worktree with nothing reachable only from it
— is recoverable from the default branch, so it is removed and pruned. A
*unique-unpushed-work* orphan carries commits absent from every remote: it is
KEPT with a warning (salvage it by pushing the branch — the snapshot-then-reap
path is gone; potentially-needed work is never destroyed). An
*uncommitted-changes* orphan (a live worktree an agent may be mid-task in) is
always KEPT — a clean removal would lose the dirty diff.

**Remote-state freshness is a precondition of every disposition here.** The
"is it on a remote?" probe is a local graph query over ``refs/remotes/*``, and
those refs go stale when a branch is deleted upstream by anything other than
this clone (a forge's auto-delete-on-merge, or a sibling clone). A stale
tracking ref makes unpushed work look pushed, which reaped genuinely-unmerged
branches. So each clone's remote-tracking refs are refreshed via
:func:`teatree.utils.git.fetch_all_prune` BEFORE any orphan in it is classified,
and a failed refresh fails CLOSED — every orphan in that clone is kept and
nothing is removed.
"""

import logging
from pathlib import Path

from teatree.core.cleanup.checkout_registry import candidate_clones
from teatree.core.cleanup.clean_ignore import is_clean_ignored
from teatree.core.cleanup.orphan_checkouts import (
    db_tracked_worktree_paths,
    orphan_checkouts_for_clone,
    orphan_has_unique_work,
    orphan_is_dirty,
)
from teatree.core.cleanup.unshipped_work import capture_unshipped_work
from teatree.core.management.commands._workspace.preview import preview_line
from teatree.core.worktree.venue_safe_registry import prune_worktrees
from teatree.utils import git
from teatree.utils.run import CommandFailedError

logger = logging.getLogger(__name__)


def _remove_orphan(repo: str, wt_path: str, branch: str) -> bool:
    """Remove the orphan worktree and prune the registry. Returns success."""
    if not git.worktree_remove(repo, wt_path):
        return False
    prune_worktrees(repo)
    if branch != git.DETACHED_HEAD:
        git.branch_delete(repo, branch)
    return True


def _reap_one_orphan(repo: str, wt_path: str, branch: str, *, dry_run: bool = False) -> str:
    """Dispose of one orphaned raw worktree under the keep-unproven-work policy.

    The capture runs before the FIRST return, so it covers every disposition —
    including the KEPT ones (a ``clean_ignore`` match among them), which is where
    work accumulated unobserved. It reads the checkout and writes elsewhere, and
    it never raises, so no verdict below depends on it; it is skipped under
    ``dry_run`` to keep a preview free of side effects.
    """
    label = f"{branch} ({wt_path})"
    if not dry_run:
        capture_unshipped_work(Path(wt_path), branch=branch)
    if is_clean_ignored(branch):
        return f"SKIPPED orphan '{label}': matches clean_ignore — keeping"
    if orphan_is_dirty(wt_path):
        return f"KEPT orphan '{label}': uncommitted changes — never reaped"
    if orphan_has_unique_work(repo, branch, wt_path):
        return f"KEPT orphan '{label}': unpushed work not on any remote — push it to salvage, never reaped"
    if dry_run:
        return preview_line(f"Reap orphan worktree (work already on remote): {label}", dry_run=True)
    if _remove_orphan(repo, wt_path, branch):
        return f"Reaped orphan worktree (work already on remote): {label}"
    return f"SKIPPED orphan '{label}': git worktree remove failed"


def reap_orphan_raw_worktrees(workspace: Path, *, dry_run: bool = False) -> list[str]:
    """Discover and dispose of raw git worktrees no ``Worktree`` row tracks (#2361).

    For every main clone teatree knows about, every linked worktree whose
    absolute path is NOT in the DB-tracked set is an orphan. Each is classified
    and disposed of by :func:`_reap_one_orphan`: a merged/gone orphan is reaped; an
    orphan with unpushed work or uncommitted changes is KEPT (never destroyed).
    The pass is resilient: a clone whose worktree registry cannot be read (corrupt
    / origin-less) is skipped with a warning rather than aborting the run.

    Before classifying ANY orphan in a clone, that clone's remote-tracking refs
    are refreshed (:func:`teatree.utils.git.fetch_all_prune`) so the
    absent-from-all-remotes probe cannot be fooled by a ref left stale by an
    upstream deletion. The fetch runs only for a clone that actually has orphan
    candidates, so a sweep with nothing to do stays offline-silent. A failed
    refresh fails CLOSED: the clone is skipped whole and none of its orphans are
    touched, because unknown remote state must never authorise a deletion.
    """
    tracked = db_tracked_worktree_paths()
    cleaned: list[str] = []
    for repo in sorted(candidate_clones(workspace)):
        try:
            orphans = orphan_checkouts_for_clone(repo, tracked)
        except CommandFailedError as exc:
            cleaned.append(f"SKIPPED clone {repo}: could not list worktrees ({exc})")
            continue
        if not orphans:
            continue
        if not git.fetch_all_prune(repo):
            cleaned.append(
                f"SKIPPED clone {repo}: could not refresh remote refs (fetch --prune failed) — "
                f"keeping {len(orphans)} orphan(s), nothing reaped"
            )
            continue
        cleaned.extend(_reap_one_orphan(repo, orphan.path, orphan.branch, dry_run=dry_run) for orphan in orphans)
    return cleaned
