"""The protection gates every reaping pass must clear before it may act on a row.

Three guards decide, ahead of any git classification, that a registered worktree is
off limits: the operator's ``clean_ignore`` protect list, the ownership exclusion (a
colleague's work on a product repo), and the liveness guard (a busy ticket, a live
external-delivery lease, a recent E2E run, an explicit ``reaper_pinned`` pin, a
process working inside the dir). They live here, in one predicate, because they are
the SAME standard for every pass — and a second pass that re-derived them from their
parts would be free to drift into a weaker one.

That is not hypothetical: :func:`~teatree.core.worktree.dead_row_release.plan_dead_row_release`
called the broken-checkout classifier directly, so a row its sibling
:func:`~teatree.core.worktree.worktree_done.reap_done_worktree` KEEPS as pinned or
mid-delivery was RELEASED by the narrow command. None of these signals is mooted by
the checkout dying — :func:`~teatree.core.cleanup.cleanup_liveness._db_liveness_reason`
reads only the database — so a dead directory is a reason to ask the gates, never a
reason to skip them.

The verdict is the gate plus its own reason; each caller renders it in its own
vocabulary (``SKIPPED`` / ``EXCLUDED`` / ``ACTIVE`` for the sweep, ``KEPT`` for the
narrow release) without re-deciding anything.
"""

from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path

from teatree.config import get_effective_settings
from teatree.core.cleanup.clean_ignore import is_clean_ignored
from teatree.core.cleanup.cleanup import _resolve_worktree_path
from teatree.core.cleanup.cleanup_liveness import worktree_liveness
from teatree.core.cleanup.cleanup_ownership import is_excluded_by_ownership
from teatree.core.models import Worktree
from teatree.core.worktree.clone_paths import resolve_clone_path

_CLEAN_IGNORE_REASON = "matches clean_ignore — keeping"


class ReapPreGate(StrEnum):
    """Which protection gate held a row back."""

    CLEAN_IGNORE = "clean-ignore"
    OWNERSHIP = "ownership"
    LIVENESS = "liveness"


@dataclass(frozen=True, slots=True)
class ReapPreGateVerdict:
    """The gate that fired and the phrase the caller reports for it."""

    gate: ReapPreGate
    reason: str


def reap_pre_gate(worktree: Worktree, *, workspace: Path, fsm_terminal: bool = False) -> ReapPreGateVerdict | None:
    """The gate protecting ``worktree`` from a reaping pass, or ``None`` when none does.

    Evaluated cheapest-first, and the first firing gate short-circuits:
    ``clean_ignore`` (a settings lookup) → ownership (one ``git log`` on a
    colleague-facing repo, a no-op on a solo one) → liveness (DB signals, then the
    filesystem ones).

    ``fsm_terminal`` is threaded to :func:`worktree_liveness` for the post-merge
    teardown, whose own ceremony trips the busy-ticket and recent-commit signals. An
    ad-hoc sweep leaves it off and keeps the full protection.
    """
    if is_clean_ignored(worktree.branch, overlay=worktree.overlay):
        return ReapPreGateVerdict(ReapPreGate.CLEAN_IGNORE, _CLEAN_IGNORE_REASON)
    repo_main = resolve_clone_path(workspace, worktree) or workspace / worktree.repo_path
    settings = get_effective_settings()
    ownership = is_excluded_by_ownership(
        str(repo_main),
        worktree.branch,
        owner_aliases=settings.user_identity_aliases,
        colleague_pattern=settings.colleague_repo_url_pattern,
    )
    if ownership.excluded:
        return ReapPreGateVerdict(ReapPreGate.OWNERSHIP, ownership.reason)
    wt_path = Path(_resolve_worktree_path(workspace, worktree))
    liveness = worktree_liveness(worktree, wt_path=wt_path, fsm_terminal=fsm_terminal)
    if liveness.active:
        return ReapPreGateVerdict(ReapPreGate.LIVENESS, liveness.reason)
    return None


__all__ = ["ReapPreGate", "ReapPreGateVerdict", "reap_pre_gate"]
