"""Liveness guard — never auto-delete or emit-for-deletion an actively-worked item (#2763).

The reaper's FIRST gate, ahead of done-detection and redundancy analysis: an
item a human or agent is mid-task in must be SKIPPED-and-reported, never wiped
and never emitted for the salvage skill to delete. A worktree/branch is LIVE
when ANY of these hold (fail-safe — uncertainty resolves to LIVE/keep): (a) its
ticket has a live :class:`Session` (``ended_at`` null) or an active
:class:`Task` (``PENDING`` / ``CLAIMED``) — the same busy-ticket signal the
idle-stack reaper uses; (b) the worktree dir is the current process CWD (an
agent is operating inside it); (c) a git lock (``index.lock``) is present in the
worktree's gitdir — git is mid-operation, so removing the worktree would corrupt
an in-flight command; (d) its HEAD commit is more recent than ``recent_minutes``
— freshly-committed work, likely still in progress.

The worktree *directory* mtime is deliberately NOT a signal: provisioning writes
the env cache into every worktree, touching the dir, so a settled worktree would
falsely read as recently-modified — the meaningful content-modification signal is
the last COMMIT. The verdict is a fail-safe phrase the reaper logs so a skip is
never silent.

The ``fsm_terminal`` carve-out (#2763 follow-up): the post-merge FSM-immediate
teardown fires the instant a ticket reaches a terminal state, and the terminal
transition itself trips two of these signals as STRUCTURAL false positives — the
merge ceremony mints the canonical phase session (busy-ticket) and writes the
merge commit (recent-commit). On that path those two are bypassed; the genuine
in-flight-operation guards (CWD, git index.lock) still fire, and the real
data-loss safety is ``analyze_worktree_changes`` (every uncommitted/unpushed
change PROVEN redundant) regardless. The ad-hoc ``clean-all`` sweep — where a
live agent really may be mid-task — keeps every signal (``fsm_terminal`` off).
"""

import logging
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path

from django.utils import timezone as dj_timezone

from teatree.core.models import Session, Task, Worktree
from teatree.utils import git
from teatree.utils.run import CommandFailedError

logger = logging.getLogger(__name__)

_ACTIVE_TASK_STATES: tuple[str, ...] = (Task.Status.PENDING, Task.Status.CLAIMED)
# A worktree touched (HEAD commit or dir mtime) within this window is treated as
# live in-progress work and kept. Generous by design — the reaper errs to keep.
_RECENT_ACTIVITY_MINUTES = 120


@dataclass(frozen=True, slots=True)
class LivenessVerdict:
    """Whether an item is actively worked, and the fail-safe reason it is kept.

    ``active`` is ``True`` when any liveness signal fired; ``reason`` is the
    human-readable phrase the reaper reports (empty only when ``active`` is
    ``False``).
    """

    active: bool
    reason: str = ""


def _ticket_is_busy(worktree: Worktree) -> bool:
    """True iff the worktree's ticket has a live session or an active/claimed task."""
    ticket = worktree.ticket
    if ticket is None:
        return False
    if Session.objects.filter(ticket=ticket, ended_at__isnull=True).exists():
        return True
    return Task.objects.filter(ticket=ticket, status__in=_ACTIVE_TASK_STATES).exists()


def _is_cwd(wt_path: Path) -> bool:
    """True iff the current process CWD is the worktree dir or a child of it."""
    try:
        cwd = Path.cwd().resolve()
    except OSError:
        return False
    resolved = wt_path.resolve()
    return cwd == resolved or resolved in cwd.parents


def _git_lock_present(wt_path: Path) -> bool:
    """True iff an ``index.lock`` exists in the worktree's gitdir (git mid-operation)."""
    if not (wt_path / ".git").exists():
        return False
    git_dir = git.run(repo=str(wt_path), args=["rev-parse", "--absolute-git-dir"])
    return bool(git_dir) and (Path(git_dir) / "index.lock").exists()


def _last_commit_at(wt_path: Path) -> datetime | None:
    """The committer timestamp of HEAD as an aware UTC datetime, or ``None``."""
    try:
        raw = git.run(repo=str(wt_path), args=["log", "-1", "--format=%ct", "HEAD"])
    except CommandFailedError:
        return None
    if not raw.strip():
        return None
    try:
        return datetime.fromtimestamp(int(raw.strip()), tz=UTC)
    except (ValueError, OSError):
        return None


def worktree_liveness(
    worktree: Worktree,
    *,
    wt_path: Path,
    now: datetime | None = None,
    recent_minutes: int = _RECENT_ACTIVITY_MINUTES,
    fsm_terminal: bool = False,
) -> LivenessVerdict:
    """Whether ``worktree`` is actively worked — fail-safe to LIVE on any signal.

    Checked in cheap-to-expensive order: busy ticket → CWD → git lock → recent
    HEAD commit. The first signal short-circuits with its reason. A not-live
    verdict means none fired, so the reaper may proceed to done-detection.

    ``fsm_terminal`` bypasses the two FSM-ceremony false positives (busy-ticket
    and recent-commit) for the post-merge teardown — see the module docstring.
    CWD and git index.lock still fire on that path.
    """
    if not fsm_terminal and _ticket_is_busy(worktree):
        return LivenessVerdict(active=True, reason="ticket has a live session or active/claimed task")
    if _is_cwd(wt_path):
        return LivenessVerdict(active=True, reason="the worktree dir is the current process CWD")
    if not wt_path.is_dir():
        return LivenessVerdict(active=False)
    if _git_lock_present(wt_path):
        return LivenessVerdict(active=True, reason="a git index.lock is present (git mid-operation)")
    if not fsm_terminal:
        moment = now or dj_timezone.now()
        cutoff = moment - timedelta(minutes=recent_minutes)
        last_commit = _last_commit_at(wt_path)
        if last_commit is not None and last_commit > cutoff:
            return LivenessVerdict(active=True, reason=f"HEAD commit within the last {recent_minutes}m")
    return LivenessVerdict(active=False)
