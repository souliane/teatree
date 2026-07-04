"""Dispatch preflight — target head-state injected into maker briefs (PR-12).

Before a coding/maker dispatch, the brief carries the ticket-worktree's current
HEAD commit relative to when the work was triggered, so the maker BUILDS ON any
commits already on the branch instead of restarting from scratch.

Robust by construction (the dispatch path must never crash on it): a missing
worktree, a non-git path, or any git failure yields no head-state block — the
brief is simply unchanged, never a raise. ``git.run`` already tolerates a
non-zero git exit (empty stdout), and the whole resolve is guarded.
"""

from dataclasses import dataclass
from datetime import UTC, datetime
from typing import TYPE_CHECKING

from teatree.core.models.ticket_worktree_checks import dispatch_worktree_path
from teatree.utils import git

if TYPE_CHECKING:
    from teatree.core.models.task import Task

# A committer-timestamp / subject record from one `git log -1`, unit-separated so
# a subject containing spaces or the field text is never mis-split.
_UNIT = "\x1f"


@dataclass(frozen=True)
class HeadState:
    """The ticket-worktree HEAD commit at dispatch time."""

    branch: str
    sha: str
    subject: str
    committed_at: datetime | None

    @property
    def short_sha(self) -> str:
        return self.sha[:12]


def resolve_head_state(task: "Task") -> "HeadState | None":
    """Read the ticket-worktree's HEAD commit, or ``None`` when unavailable.

    ``None`` when the ticket has no materialised worktree yet (pre-provision),
    the path is not a git repo, or the log read comes back empty — every case
    where there is simply no head-state to report.
    """
    worktree = dispatch_worktree_path(task.ticket)
    if not worktree:
        return None
    try:
        line = git.run(repo=worktree, args=["log", "-1", f"--format=%H{_UNIT}%ct{_UNIT}%s"])
    except OSError:
        return None
    match line.split(_UNIT, maxsplit=2):
        case [sha, epoch, subject] if sha:
            return HeadState(
                branch=_branch(worktree),
                sha=sha,
                subject=subject,
                committed_at=_epoch_to_utc(epoch),
            )
        case _:
            return None


def head_state_brief_lines(task: "Task") -> tuple[str, ...]:
    """Render the head-state preflight block for a maker brief, or ``()``.

    Empty when there is no head-state to report — a fresh ticket with no
    worktree, or a non-git path — so a dispatch is byte-identical to today
    until a provisioned branch actually carries a commit.
    """
    state = resolve_head_state(task)
    if state is None:
        return ()
    committed = state.committed_at.isoformat() if state.committed_at else "unknown time"
    return (
        "",
        "DISPATCH PREFLIGHT — target head state (build on this, do NOT restart from scratch):",
        f"  branch: {state.branch or '(detached)'}",
        f'  HEAD: {state.short_sha} "{state.subject}" (committed {committed})',
        f"  {_trigger_line(state, getattr(task, 'created_at', None))}",
    )


def _trigger_line(state: HeadState, triggered_at: "datetime | None") -> str:
    if state.committed_at is None or triggered_at is None:
        return "Inspect `git log` before coding; continue from HEAD rather than re-implementing landed work."
    if state.committed_at >= triggered_at:
        return (
            f"HEAD landed AFTER this dispatch was triggered ({triggered_at.isoformat()}) — "
            "work already exists on the branch in this cycle; continue from HEAD."
        )
    return (
        f"HEAD predates this dispatch ({triggered_at.isoformat()}) — the branch is at its "
        "pre-dispatch state; no commits yet in this cycle."
    )


def _branch(worktree: str) -> str:
    try:
        return git.current_branch(worktree)
    except (OSError, ValueError, RuntimeError):
        return ""


def _epoch_to_utc(epoch: str) -> datetime | None:
    try:
        return datetime.fromtimestamp(int(epoch), tz=UTC)
    except (ValueError, OverflowError, OSError):
        return None
