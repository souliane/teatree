"""Liveness guard — never auto-delete or emit-for-deletion an actively-worked item (#2763).

The reaper's FIRST gate, ahead of done-detection and redundancy analysis: an
item a human or agent is mid-task in must be SKIPPED-and-reported, never wiped
and never emitted for the salvage skill to delete. A worktree/branch is LIVE
when ANY of these hold (fail-safe — uncertainty resolves to LIVE/keep): (a) its
ticket has a live :class:`Session` (``ended_at`` null) or an active
:class:`Task` (``PENDING`` / ``CLAIMED``) — the same busy-ticket signal the
idle-stack reaper uses; (b) the ticket carries a live external-delivery lease, a
recent E2E/evidence run touched the worktree, or it is explicitly
``reaper_pinned`` — the shared #2227/#2773 active-delivery guards, folded in via
:func:`teatree.core.gates.idle_stack.active_delivery_keep_reason` so this reaper
never protects LESS than the reversible idle-stack reaper; (c) ANY live
process' CWD is inside the worktree dir (an agent is operating inside it) —
scanned via ``/proc/*/cwd`` on Linux, not just the reaper's own process; (d) a git lock
(``index.lock``) is present in the worktree's gitdir — git is mid-operation, so
removing the worktree would corrupt an in-flight command; (e) its HEAD commit is
more recent than ``recent_minutes`` — freshly-committed work, likely still in
progress.

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
in-flight-operation guards (CWD, git index.lock) AND the active-delivery guards
(external-delivery lease / recent E2E / ``reaper_pinned`` — the merge mints none
of these) still fire, and the real data-loss safety is ``analyze_worktree_changes``
(every uncommitted/unpushed change PROVEN redundant) regardless. The ad-hoc
``clean-all`` sweep — where a live agent really may be mid-task — keeps every
signal (``fsm_terminal`` off).
"""

import logging
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path

from django.utils import timezone as dj_timezone

from teatree.core.gates.idle_stack import active_delivery_keep_reason
from teatree.core.models import Worktree
from teatree.utils import git
from teatree.utils.run import CommandFailedError

logger = logging.getLogger(__name__)

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
    return ticket is not None and ticket.has_active_work()


_PROC_ROOT = Path("/proc")


def _within(cwd: Path, resolved_wt: Path) -> bool:
    """True iff ``cwd`` is the worktree dir itself or a directory inside it."""
    return cwd == resolved_wt or resolved_wt in cwd.parents


def _is_cwd(wt_path: Path) -> bool:
    """True iff ANY live process' CWD is the worktree dir or a child of it.

    An agent operating inside a worktree — a shell, an editor, a dev server — has
    its CWD there. Checking only the reaper's OWN process CWD misses that ad-hoc
    agent entirely, so on Linux this also scans ``/proc/*/cwd`` for any process
    working inside the worktree. Best-effort: a process whose ``cwd`` symlink is
    unreadable (permission, race) is skipped, and a platform without ``/proc``
    falls back to the own-CWD check.
    """
    try:
        own = Path.cwd().resolve()
    except OSError:
        own = None
    resolved = wt_path.resolve()
    if own is not None and _within(own, resolved):
        return True
    return _any_process_cwd_within(resolved)


def _any_process_cwd_within(resolved_wt: Path) -> bool:
    """Scan ``/proc/*/cwd`` for a process whose working dir is inside ``resolved_wt``."""
    if not _PROC_ROOT.is_dir():
        return False
    for entry in _PROC_ROOT.iterdir():
        if not entry.name.isdigit():
            continue
        try:
            cwd = (entry / "cwd").resolve()
        except OSError:
            continue
        if _within(cwd, resolved_wt):
            return True
    return False


def _git_lock_present(wt_path: Path) -> bool:
    """True iff an ``index.lock`` exists in the worktree's gitdir (git mid-operation)."""
    if not (wt_path / ".git").exists():
        return False
    git_dir = git.run(repo=str(wt_path), args=["rev-parse", "--absolute-git-dir"])
    return bool(git_dir) and (Path(git_dir) / "index.lock").exists()


def _last_commit_at(wt_path: Path) -> datetime | None:
    """The committer timestamp of HEAD as an aware UTC datetime, or ``None``.

    Runs the STRICT runner so a real ``git log`` failure (no HEAD, corrupt repo)
    raises and is caught HONESTLY to ``None`` — with the lenient runner the
    ``except CommandFailedError`` was dead code and a failure silently returned
    ``""`` down the same ``None`` path, so the handler now actually fires.
    """
    try:
        raw = git.run_strict(repo=str(wt_path), args=["log", "-1", "--format=%ct", "HEAD"])
    except CommandFailedError:
        return None
    if not raw.strip():
        return None
    try:
        return datetime.fromtimestamp(int(raw.strip()), tz=UTC)
    except (ValueError, OSError):
        return None


def _db_liveness_reason(worktree: Worktree, *, now: datetime | None, fsm_terminal: bool) -> str | None:
    """The DB-only liveness signals: busy ticket (FSM-ceremony-gated) + active delivery.

    ``fsm_terminal`` bypasses the busy-ticket false positive (the merge mints the
    phase session). The #2227/#2773 active-delivery guards — a live
    external-delivery lease, a recent E2E/evidence run, or an explicit
    ``reaper_pinned`` pin — are folded in from the shared idle-stack predicate and
    fire UNCONDITIONALLY: unlike busy-ticket they are NOT minted by the merge, so a
    worktree delivering externally / freshly e2e-tested / pinned is KEPT through the
    post-merge teardown too, and this reaper never protects LESS than the reversible
    idle-stack reaper.
    """
    if not fsm_terminal and _ticket_is_busy(worktree):
        return "ticket has a live session or active/claimed task"
    return active_delivery_keep_reason(worktree, now=now)


def _fs_liveness_reason(*, wt_path: Path, now: datetime | None, recent_minutes: int, fsm_terminal: bool) -> str | None:
    """The filesystem liveness signals: CWD, git index.lock, recent HEAD commit.

    A missing worktree dir contributes no filesystem signal (``None``). ``recent
    HEAD commit`` is bypassed on ``fsm_terminal`` (the merge commit is the false
    positive); CWD and git index.lock fire on every path.
    """
    if _is_cwd(wt_path):
        return "the worktree dir is the current process CWD"
    if not wt_path.is_dir():
        return None
    if _git_lock_present(wt_path):
        return "a git index.lock is present (git mid-operation)"
    if not fsm_terminal:
        moment = now or dj_timezone.now()
        cutoff = moment - timedelta(minutes=recent_minutes)
        last_commit = _last_commit_at(wt_path)
        if last_commit is not None and last_commit > cutoff:
            return f"HEAD commit within the last {recent_minutes}m"
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

    Checked in cheap-to-expensive order: the DB-only signals (busy ticket →
    active-delivery, :func:`_db_liveness_reason`) then the filesystem signals (CWD
    → git lock → recent HEAD commit, :func:`_fs_liveness_reason`). The first signal
    short-circuits with its reason. A not-live verdict means none fired, so the
    reaper may proceed to done-detection.

    ``fsm_terminal`` bypasses the two FSM-ceremony false positives (busy-ticket
    and recent-commit) for the post-merge teardown — see the module docstring.
    CWD, git index.lock, and the #2227/#2773 active-delivery guards still fire on
    that path.
    """
    reason = _db_liveness_reason(worktree, now=now, fsm_terminal=fsm_terminal) or _fs_liveness_reason(
        wt_path=wt_path, now=now, recent_minutes=recent_minutes, fsm_terminal=fsm_terminal
    )
    return LivenessVerdict(active=reason is not None, reason=reason or "")
