"""The dirty-worktree KEEP guard, split out of :mod:`teatree.core.cleanup.cleanup`.

Lives in its own module so the teardown orchestrator stays under the module-health
LOC cap (mirrors ``cleanup_busy_guards`` and ``cleanup_orphan_ref``). The probe it
consults is :mod:`teatree.core.cleanup.working_tree_dirt`; what this module adds is
the DECISION — keep or proceed — and the sentence the operator reads for it.

Two outcomes keep the worktree and they are reported differently, because they are
different findings: files that are modified, and a probe that could not read the
working tree at all. Rendering the second as the first sends an operator hunting
for edits nothing ever saw.
"""

import logging
from typing import TYPE_CHECKING

from teatree.core.cleanup.working_tree_dirt import WorkingTreeDirt, working_tree_dirt
from teatree.core.models import Worktree

if TYPE_CHECKING:
    from teatree.core.cleanup.cleanup import _EffectiveTarget

logger = logging.getLogger(__name__)


def guard_or_warn_dirty_worktree(
    worktree: Worktree, wt_path: str, target: "_EffectiveTarget", *, keep_if_dirty: bool, force: bool
) -> None:
    """KEEP a dirty worktree when ``keep_if_dirty`` (the fail-closed default), else warn-and-proceed.

    A worktree with uncommitted changes may be a live one an agent is mid-task
    in, and those edits are on no remote — a board "Done" event or an unattended
    teardown would wipe them with no salvage. ``keep_if_dirty`` defaults ``True``
    (fail-closed): a dirty worktree raises ``RuntimeError`` before any destructive
    step, which the reaper / sync backend routes to a KEEP-with-warning. Only an
    explicit ``keep_if_dirty=False`` caller warns-and-proceeds, and ``force=True``
    (the proven-redundant reaper / explicit abandon) overrides the guard entirely.

    "Dirty" is REAL uncommitted work — :func:`working_tree_dirt` ignores the
    regenerable env cache every provisioned worktree carries and the "every tracked
    file reads as a staged add" noise of a dangling-HEAD (post-merge branch-ref
    deletion) worktree. A raw ``git status --porcelain`` would false-positive on
    both, refusing teardown of every normally-provisioned worktree and every
    legitimate post-merge orphan; the shared probe fails CLOSED only on GENUINE
    edits.
    """
    dirt = working_tree_dirt(wt_path, target)
    if not dirt.reasons:
        return
    if keep_if_dirty and not force:
        raise RuntimeError(kept_worktree_message(worktree, wt_path, dirt))
    if dirt.proven:
        logger.warning("%s has uncommitted changes — cleaning anyway (PR merged)", worktree.repo_path)
    else:
        logger.warning(
            "%s: the working-tree probe could not answer (%s) — cleaning anyway (PR merged)",
            worktree.repo_path,
            "; ".join(dirt.reasons),
        )


def kept_worktree_message(worktree: Worktree, wt_path: str, dirt: WorkingTreeDirt) -> str:
    """The refusal an operator reads — proven dirt and an unanswerable probe say different things."""
    head = f"{worktree.repo_path} ({worktree.branch}): "
    if dirt.proven:
        return (
            f"{head}refused cleanup — worktree has uncommitted changes (possibly in use): "
            f"{'; '.join(dirt.reasons)}. Kept it on disk at {wt_path}; commit or discard the changes, "
            "then re-run cleanup."
        )
    return (
        f"{head}kept, unverified — the working-tree probe could not answer: {'; '.join(dirt.reasons)}. "
        f"This is not proof of uncommitted work, so nothing was destroyed and nothing was concluded about "
        f"{wt_path}; re-run once the probe can answer, or force the teardown to override it."
    )


__all__ = ["guard_or_warn_dirty_worktree", "kept_worktree_message"]
