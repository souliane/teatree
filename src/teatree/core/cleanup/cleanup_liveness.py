"""Liveness guard — never auto-delete or emit-for-deletion an actively-worked item (#2763).

The reaper's FIRST gate, ahead of done-detection and redundancy analysis: an
item a human or agent is mid-task in must be SKIPPED-and-reported, never wiped
and never emitted for the salvage skill to delete. A worktree/branch is LIVE
when ANY of these hold (fail-safe — uncertainty resolves to LIVE/keep): (a) its
ticket has a live :class:`Session` (open AND active within
``session_stale_after_hours`` — an abandoned session must not pin a ticket
forever) or an active :class:`Task` (``PENDING`` / ``CLAIMED``, no time bound)
— the same busy-ticket signal the idle-stack reaper uses; (b) the ticket
carries a live external-delivery lease, a
recent E2E/evidence run touched the worktree, or it is explicitly
``reaper_pinned`` — the shared #2227/#2773 active-delivery guards, folded in via
:func:`teatree.core.gates.idle_stack.active_delivery_keep_reason` so this reaper
never protects LESS than the reversible idle-stack reaper; (c) ANY live
process is placed inside the worktree dir (an agent is operating inside it) —
read from the shared host-aware :mod:`teatree.core.cleanup.process_table`, not
just the reaper's own process and not this container's namespace; (d) a git lock
(``index.lock``) is present in the worktree's gitdir — git is mid-operation, so
removing the worktree would corrupt an in-flight command; (e) its HEAD commit is
more recent than ``recent_minutes`` — freshly-committed work, likely still in
progress.

The worktree *directory* mtime is deliberately NOT a signal: provisioning writes
the env cache into every worktree, touching the dir, so a settled worktree would
falsely read as recently-modified — the meaningful content-modification signal is
the last COMMIT. The verdict is a fail-safe phrase the reaper logs so a skip is
never silent.

Guards (c) and (d) can each be structurally unable to look — a venue with no
host-covering process table, a gitdir that will not resolve — and both used to
answer that with the same bytes as "I looked and found nothing" (#4354). The
verdict now carries ``unverifiable`` and the obstacle, and the blindness is
logged. It does not promote to a keep: this reaper proves every change redundant
before wiping anything, so an absent defence-in-depth guard is reported rather
than turned into a reap-stopping signal.

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

from teatree.core.cleanup.process_table import read_process_table
from teatree.core.gates.idle_stack import active_delivery_keep_reason
from teatree.core.models import Worktree
from teatree.utils import git
from teatree.utils.git_run import run_with_status
from teatree.utils.run import CommandFailedError
from teatree.utils.throttled_log import warn_throttled

logger = logging.getLogger(__name__)

# A worktree touched (HEAD commit or dir mtime) within this window is treated as
# live in-progress work and kept. Generous by design — the reaper errs to keep.
_RECENT_ACTIVITY_MINUTES = 120


@dataclass(frozen=True, slots=True)
class LivenessVerdict:
    """Whether an item is actively worked, and the fail-safe reason it is kept.

    ``active`` is ``True`` when any liveness signal fired; ``reason`` is the
    human-readable phrase the reaper reports.

    ``unverifiable`` is the sibling of :attr:`~teatree.core.cleanup.working_tree_dirt.WorkingTreeDirt.proven`
    (#4354): ``True`` when a guard could not ANSWER — the venue saw no process
    table, the gitdir would not resolve — and ``reason`` then names the obstacle
    rather than a signal. A not-live verdict and a could-not-look verdict were the
    same empty bytes, so a guard that is structurally absent in the deployment the
    reaper runs in reported exactly like a settled worktree. It does not by itself
    keep the item: this reaper carries several independent guards and proves every
    change redundant before wiping, so the blindness is REPORTED, not promoted into
    a keep (the same split :mod:`teatree.core.cleanup.process_table` records for its
    two consumers).
    """

    active: bool
    reason: str = ""
    unverifiable: bool = False


@dataclass(frozen=True, slots=True)
class _GuardAnswer:
    """One guard's answer: it fired, it did not, or it could not look.

    ``fired`` with an empty ``obstacle`` is a real negative; an ``obstacle`` with
    ``fired`` false is the non-answer the verdict must carry upward.
    """

    fired: bool = False
    obstacle: str = ""

    @property
    def unanswered(self) -> bool:
        return not self.fired and bool(self.obstacle)


def _ticket_is_busy(worktree: Worktree) -> bool:
    """True iff the worktree's ticket has a live session or an active/claimed task."""
    ticket = worktree.ticket
    return ticket is not None and ticket.has_active_work()


def _within(cwd: Path, resolved_wt: Path) -> bool:
    """True iff ``cwd`` is the worktree dir itself or a directory inside it."""
    return cwd == resolved_wt or resolved_wt in cwd.parents


def _is_cwd(wt_path: Path) -> _GuardAnswer:
    """Whether ANY live process' CWD is the worktree dir or a child of it.

    An agent operating inside a worktree — a shell, an editor, a dev server — has
    its CWD there. Checking only the reaper's OWN process CWD misses that ad-hoc
    agent entirely, so the shared process table is consulted for any process
    working inside the worktree. The own-CWD hit is decisive on its own; otherwise
    the answer (including "I could not look") is the table's.
    """
    try:
        own = Path.cwd().resolve()
    except OSError:
        own = None
    resolved = wt_path.resolve()
    if own is not None and _within(own, resolved):
        return _GuardAnswer(fired=True)
    return _any_process_cwd_within(wt_path)


def _any_process_cwd_within(wt_path: Path) -> _GuardAnswer:
    """Whether any live process is placed inside ``wt_path``.

    Reads the shared host-aware table (:mod:`teatree.core.cleanup.process_table`)
    rather than this venue's own ``/proc``, which inside the worker container
    lists a namespace holding none of the host agents this signal exists to spot.
    A table that could not be read is reported as an OBSTACLE rather than a
    negative: it still contributes no keep — unlike the venv reaper, this reaper
    proves every change redundant before wiping anything — but the caller can now
    tell "nobody is inside" from "nobody could be seen from here".

    The path goes in AS WRITTEN: the table matches both spellings itself, and
    pre-resolving here would throw away the raw one.
    """
    table = read_process_table()
    if not table.usable:
        return _GuardAnswer(obstacle=f"no process could be seen from this venue ({table.refuse_reason()})")
    return _GuardAnswer(fired=table.holds(wt_path))


def _git_lock_present(wt_path: Path) -> _GuardAnswer:
    """Whether an ``index.lock`` exists in the worktree's gitdir (git mid-operation).

    Runs the status-carrying runner: :func:`teatree.utils.git.run` collapses a failed
    ``rev-parse`` and an empty answer onto ``""``, so a gitdir that would not resolve
    — a linked checkout whose admin path this venue cannot reach — read as a
    confident "no lock is present" over a lock sitting on disk.
    """
    if not (wt_path / ".git").exists():
        return _GuardAnswer()
    result = run_with_status(repo=str(wt_path), args=["rev-parse", "--absolute-git-dir"])
    git_dir = result.stdout.strip()
    if result.returncode != 0 or not git_dir:
        return _GuardAnswer(obstacle="the worktree's gitdir would not resolve, so an index.lock is invisible here")
    return _GuardAnswer(fired=(Path(git_dir) / "index.lock").exists())


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


def _fs_liveness(*, wt_path: Path, now: datetime | None, recent_minutes: int, fsm_terminal: bool) -> LivenessVerdict:
    """The filesystem liveness signals: CWD, git index.lock, recent HEAD commit.

    A missing worktree dir contributes no filesystem signal. ``recent HEAD commit``
    is bypassed on ``fsm_terminal`` (the merge commit is the false positive); CWD
    and git index.lock fire on every path. A guard that could not answer leaves its
    obstacle on the not-live verdict (``unverifiable``) instead of vanishing into it.
    """
    blind: list[str] = []
    cwd = _is_cwd(wt_path)
    if cwd.fired:
        return LivenessVerdict(active=True, reason="the worktree dir is the current process CWD")
    if cwd.unanswered:
        blind.append(cwd.obstacle)
    if wt_path.is_dir():
        lock = _git_lock_present(wt_path)
        if lock.fired:
            return LivenessVerdict(active=True, reason="a git index.lock is present (git mid-operation)")
        if lock.unanswered:
            blind.append(lock.obstacle)
        if not fsm_terminal:
            moment = now or dj_timezone.now()
            cutoff = moment - timedelta(minutes=recent_minutes)
            last_commit = _last_commit_at(wt_path)
            if last_commit is not None and last_commit > cutoff:
                return LivenessVerdict(active=True, reason=f"HEAD commit within the last {recent_minutes}m")
    return LivenessVerdict(active=False, reason="; ".join(blind), unverifiable=bool(blind))


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
    → git lock → recent HEAD commit, :func:`_fs_liveness`). The first signal
    short-circuits with its reason. A not-live verdict means none fired, so the
    reaper may proceed to done-detection.

    ``fsm_terminal`` bypasses the two FSM-ceremony false positives (busy-ticket
    and recent-commit) for the post-merge teardown — see the module docstring.
    CWD, git index.lock, and the #2227/#2773 active-delivery guards still fire on
    that path.

    A not-live verdict whose guards could not all LOOK carries ``unverifiable`` and
    the obstacle, and says so in the log (#4354) — the reaper proceeds on its
    remaining guards, but a defence-in-depth guard that is structurally absent in
    this venue no longer reports as one that ran and found nothing.
    """
    db_reason = _db_liveness_reason(worktree, now=now, fsm_terminal=fsm_terminal)
    if db_reason is not None:
        return LivenessVerdict(active=True, reason=db_reason)
    verdict = _fs_liveness(wt_path=wt_path, now=now, recent_minutes=recent_minutes, fsm_terminal=fsm_terminal)
    if verdict.unverifiable:
        warn_throttled(
            logger,
            f"liveness-blind:{verdict.reason}",
            "liveness guard could not answer for %s: %s — proceeding on the remaining guards",
            wt_path,
            verdict.reason,
        )
    return verdict
