"""The worktree-liveness KEEP guard, split out of :mod:`teatree.core.cleanup.cleanup`.

Lives in its own module so the teardown orchestrator stays under the
module-health LOC cap (mirrors ``cleanup_orphan_ref``). :func:`guard_live_worktree`
is the LIVENESS funnel every OPPORTUNISTIC reaper routes through: it
short-circuits :func:`teatree.core.cleanup.cleanup.cleanup_worktree` with
:class:`WorktreeBusyError` BEFORE any destructive step when the ticket has a live
session, an active/claimed task, an external-delivery lease, a recent E2E run, or
an explicit ``reaper_pinned`` pin.

The dirty-worktree guard and the committed-but-unpushed DATA-LOSS guards are a
different concern — they gate the git-removal step on on-disk / remote *git*
state — and stay in :mod:`teatree.core.cleanup.cleanup` next to the removal they protect.
"""

from teatree.core.gates.idle_stack import worktree_protects_against_reap
from teatree.core.models import Worktree


class WorktreeBusyError(RuntimeError):
    """Raised by :func:`cleanup_worktree` when an OPPORTUNISTIC reap hits live work.

    A subclass of ``RuntimeError`` so existing ``except RuntimeError`` teardown
    handlers still catch it, while callers that want a distinct
    keep-with-warning (vs. the unsynced-work push/abandon path) can catch it
    specifically. Carries the human-readable keep reason from
    :func:`teatree.core.gates.idle_stack.worktree_protects_against_reap`.
    """


def guard_live_worktree(worktree: Worktree, *, respect_liveness: bool, force: bool) -> None:
    """KEEP a worktree whose ticket has live work, before any destructive step (#291/#2243).

    The funnel liveness guard every teardown caller routes through. An
    OPPORTUNISTIC reaper (clean-all, clean-merged, merge-sync cleanup,
    orphan-isolated-root) leaves ``respect_liveness`` on, so a worktree under
    live work — a live session, an active/claimed task, an external-delivery
    lease, a recent E2E run, or an explicit ``reaper_pinned`` — raises
    :class:`WorktreeBusyError` before docker-down / git-removal / DB-drop, and
    the caller keeps it with a warning. The IRREVERSIBLE teardown therefore never
    protects LESS than the REVERSIBLE idle-stack reaper.

    Explicit/FSM-driven teardown (``respect_liveness=False`` — the FSM has
    decided to tear this worktree down) and explicit abandon (``force=True``)
    bypass the guard. The data-loss guards (#706 unpushed, dirty-worktree) are
    orthogonal and still apply on those paths.
    """
    if force or not respect_liveness:
        return
    reason = worktree_protects_against_reap(worktree)
    if reason is not None:
        msg = f"{worktree.repo_path} ({worktree.branch}): kept — {reason} (live work)"
        raise WorktreeBusyError(msg)
