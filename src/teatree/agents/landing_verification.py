"""Verify a coding/debugging result actually landed a commit (root-cause gate).

The coder-yield stall: a coding sub-agent spawns a background test agent and
yields with no terminal ResultMessage, or reports ``files_modified`` while
nothing was committed. ``PHASE_REQUIRED_EVIDENCE["coding"]`` is self-reported —
it proves the agent *claimed* file changes, not that they landed. This gate
re-reads the ticket worktree's git state at completion time and refuses with a
``landing_unverified`` failure unless a new commit actually exists.

Fail-open on the unverifiable: when the ticket has no materialised worktree on
disk (a headless run with no checkout, a pre-provision task), there is nothing
to verify, so the gate returns ``""`` and completion proceeds as before —
"couldn't determine" is never "did not land" (the same posture as
:func:`teatree.core.models.ticket_worktree_checks.worktree_tracked_dirty_path`).

A worktree that IS on disk but whose commit-count probe cannot answer is the
same fail-open case, reached by a different route. The probe raises
``WorktreeProbeUnverifiableError`` there so its FSM consumer can hold the tick
and retry; this gate runs *after* the agent finished, so it has no tick to hold
— letting the error escape would record the completed run as a crash and throw
its work away. UNKNOWN is therefore absorbed here: the gate refuses only when
every checkable worktree is VERIFIABLY not ahead.
"""

from pathlib import Path
from typing import TYPE_CHECKING

from teatree.core.modelkit.phases import normalize_phase
from teatree.core.models.ticket_worktree_checks import (
    WorktreeProbeUnverifiableError,
    worktree_has_commits_ahead,
    worktree_tracked_dirty_path,
)
from teatree.core.models.worktree import Worktree

if TYPE_CHECKING:
    from teatree.core.models.task import Task

_LANDING_VERIFIED_PHASES = frozenset({"coding", "debugging"})

_UNVERIFIED_PREFIX = "landing_unverified:"


def landing_verification_error(task: "Task", *, phase: str = "") -> str:
    """Return a ``landing_unverified:`` error if a coding/debugging result did not land, else ``""``.

    Refuses when a checkable ticket worktree has uncommitted tracked changes (the
    files were edited but never committed) or every checkable worktree is
    verifiably not ahead of its base (HEAD never advanced). A non-coding/debugging
    phase, a ticket with no checkable worktree, or any worktree whose probe cannot
    answer, is a no-op (``""``).
    """
    if normalize_phase(phase or task.phase) not in _LANDING_VERIFIED_PHASES:
        return ""
    checkable = [wt for wt in Worktree.objects.for_ticket(task.ticket) if _on_disk_repo(wt)]
    if not checkable:
        return ""
    for wt in checkable:
        dirty_path = worktree_tracked_dirty_path(wt)
        if dirty_path is not None:
            return f"{_UNVERIFIED_PREFIX} uncommitted tracked changes in {dirty_path} — files_modified never committed"
    if not all(commits_ahead_or_unknown(wt) is False for wt in checkable):
        return ""
    branch = checkable[0].branch or "(detached)"
    return f"{_UNVERIFIED_PREFIX} no new commit on {branch} — HEAD has not advanced past the base"


def commits_ahead_or_unknown(worktree: "Worktree") -> bool | None:
    """Whether *worktree* has commits ahead, or ``None`` when its probe cannot answer.

    The tri-state accessor for consumers that run after an agent has finished, so
    they cannot hold the tick the raising probe is designed for. It lives here
    rather than beside the probe deliberately: flattening UNKNOWN on the FSM path
    is the #F1.4 defect that terminally abandons a live ticket, and a helper that
    does it must not be within easy reach of that caller.
    """
    try:
        return worktree_has_commits_ahead(worktree)
    except WorktreeProbeUnverifiableError:
        return None


def _on_disk_repo(worktree: "Worktree") -> bool:
    """Whether *worktree* has a materialised on-disk path a git check can read."""
    extra = worktree.extra if isinstance(worktree.extra, dict) else {}
    path = extra.get("worktree_path") or worktree.repo_path
    return bool(path) and Path(path).is_dir()
