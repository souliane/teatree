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

from teatree.core.models.plan_adequacy import declared_seam_paths
from teatree.core.models.plan_artifact import PlanArtifact
from teatree.core.models.ticket_worktree_checks import dispatch_worktree_path
from teatree.core.worktree.branch_currency import fetch_target_head, predict_merge_conflicts
from teatree.core.worktree.target_branch import resolve_target_branch
from teatree.utils import git
from teatree.utils.run import CommandFailedError

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


def declared_seams_brief_lines(task: "Task") -> tuple[str, ...]:
    """Render the plan's declared integration seams for a maker/reviewer brief, or ``()``.

    SELFCATCH-3 data-plumb: the plan-adequacy manifest names the
    registries/contracts/sibling-paths the change touches. Surfacing them in the
    coding brief tells the maker exactly which seams to wire to the consumer end;
    in the review brief it tells the reviewer which seams to verify. Pure data —
    ``()`` when the ticket has no plan or the plan declared no seams, so a dispatch
    is byte-identical to today until a manifest actually names seams.
    """
    artifact = PlanArtifact.objects.filter(ticket=task.ticket).order_by("-recorded_at", "-pk").first()
    if artifact is None:
        return ()
    seams = declared_seam_paths(artifact.adequacy)
    if not seams:
        return ()
    header = (
        "PLAN — declared integration seams (wire each producer end to its consumer; a diff touching an "
        "UNDECLARED seam is a gap):"
    )
    return ("", header, *(f"  - {seam}" for seam in seams))


_MAX_REVIEW_DIFF_CHARS = 24000


def review_diff_brief_lines(task: "Task") -> tuple[str, ...]:
    """Render the branch diff for a reviewing brief, or ``()`` (corr-11).

    A headless reviewing phase is denied the shell (PR-11), so it cannot run
    ``git diff`` itself — the diff is injected here at prompt-build time (the
    orchestrator has shell access), the same read-git-at-build-time pattern as
    :func:`head_state_brief_lines`. Empty when the ticket has no materialised
    worktree (e.g. a remote-PR review with no local checkout) or the diff comes
    back empty, so a dispatch is byte-identical to today until a branch actually
    carries changes. Truncated to keep a large diff from blowing the prompt
    budget — the reviewer Reads the changed files for anything past the cap.
    """
    diff = _resolve_branch_diff(task)
    if not diff.strip():
        return ()
    body = diff[:_MAX_REVIEW_DIFF_CHARS]
    if len(diff) > _MAX_REVIEW_DIFF_CHARS:
        body += "\n… (diff truncated — Read the changed files directly for the remainder)"
    return (
        "",
        "DIFF UNDER REVIEW (this phase has no shell — review THIS diff; Read the files for more context):",
        "```diff",
        body,
        "```",
    )


def _resolve_branch_diff(task: "Task") -> str:
    worktree = dispatch_worktree_path(task.ticket)
    if not worktree:
        return ""
    try:
        return git.branch_diff(repo=worktree)
    except (OSError, CommandFailedError):
        # A non-git path or a missing default-branch ref (merge_base failure) is
        # "no diff to inject", never a dispatch-crashing raise.
        return ""


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


@dataclass(frozen=True)
class BranchCurrency:
    """One dispatch-time reading of whether the ticket branch still merges.

    ``verified`` is ``False`` when the target could not be fetched OR the
    behind-count could not be read — two causes, one consequence: currency is
    UNSTATED. Collapsing either into ``behind_count=0`` would render a stale
    read as "current", the precise false clean this block exists to prevent.
    """

    branch: str
    target: str
    head_sha: str
    behind_count: int
    conflicting_paths: tuple[str, ...] | None
    verified: bool

    @property
    def short_sha(self) -> str:
        return self.head_sha[:12]


def resolve_branch_currency(task: "Task") -> "BranchCurrency | None":
    """Read the ticket branch's mergeability against its target; ``None`` with no worktree.

    Fetches through :func:`fetch_target_head` rather than
    :func:`~teatree.core.worktree.branch_currency.branch_behind_target` because
    the latter returns ``None`` for BOTH a failed fetch and an already-current
    branch, and telling those apart is the whole contract here.
    """
    worktree = dispatch_worktree_path(task.ticket)
    if not worktree:
        return None
    branch = _branch(worktree)
    target = _target_ref(task, worktree, branch)
    head = _head_sha(worktree)
    behind = _behind_count(worktree, target) if fetch_target_head(worktree, target) else None
    if behind is None:
        return BranchCurrency(branch, target, head, 0, None, verified=False)
    conflicts = predict_merge_conflicts(worktree, "HEAD", target) if behind > 0 else ()
    return BranchCurrency(branch, target, head, behind, conflicts, verified=True)


_CURRENCY_HEADER = "DISPATCH PREFLIGHT — branch currency (a clean local tree is NOT proof of mergeability):"

_UNVERIFIED_HINT = (
    "  Run `git fetch origin` and `gh pr view <n> --repo <slug> --json mergeable,mergeStateStatus` "
    "yourself BEFORE running any gate."
)


def branch_currency_brief_lines(task: "Task") -> tuple[str, ...]:
    """Render the testing brief's branch-currency block — never empty.

    A missing worktree, a failed fetch and an unreadable count each render an
    explicit UNVERIFIED directive instead of ``()``: an omitted block reads as
    "checked, nothing to report", which is the silent-empty degradation
    ``/t3:rules`` § "External Read Failure Must Fail Loud" forbids.
    """
    currency = resolve_branch_currency(task)
    if currency is None:
        return ("", _CURRENCY_HEADER, "  UNVERIFIED — no ticket worktree materialised at dispatch.", _UNVERIFIED_HINT)
    if not currency.verified:
        return (
            "",
            _CURRENCY_HEADER,
            f"  UNVERIFIED — could not read {currency.branch or 'HEAD'} against {currency.target} at dispatch.",
            _UNVERIFIED_HINT,
        )
    if currency.behind_count == 0:
        return (
            "",
            _CURRENCY_HEADER,
            f"  {currency.branch or 'HEAD'} is current with {currency.target} (fetched at dispatch).",
        )
    return ("", _CURRENCY_HEADER, *_behind_lines(currency))


def _merge_directive(target: str) -> tuple[str, ...]:
    return (
        "  Merge it in BEFORE gating — `t3 tool verify-gates` judges this tree against itself, never",
        f"  whether it still merges. Run `git merge --no-edit {target}` (merge, NEVER rebase), re-run",
        "  the targeted tests and `t3 tool verify-gates` on the MERGED tree, then commit the merge.",
    )


def _behind_lines(currency: "BranchCurrency") -> tuple[str, ...]:
    behind = f"  HEAD {currency.short_sha} is {currency.behind_count} commit(s) behind {currency.target}"
    directive = _merge_directive(currency.target)
    if currency.conflicting_paths is None:
        return (f"{behind}; conflict prediction unavailable.", _UNVERIFIED_HINT, *directive)
    if not currency.conflicting_paths:
        return (f"{behind} and merges clean.", *directive)
    return (
        f"{behind} and CONFLICTS on: {', '.join(currency.conflicting_paths)}",
        *directive,
        "  Resolve each conflicting path by re-deriving intent from the ticket + commit message (never",
        "  a blind ours/theirs), then grep the tree for every consumer of what the conflict touched.",
    )


def _target_ref(task: "Task", worktree: str, branch: str) -> str:
    try:
        return resolve_target_branch(task.ticket, worktree, branch=branch)
    except (OSError, CommandFailedError):
        return "origin/main"


def _head_sha(worktree: str) -> str:
    try:
        return git.head_sha(repo=worktree)
    except (OSError, CommandFailedError):
        return ""


def _behind_count(worktree: str, target: str) -> int | None:
    """``HEAD..target`` commit count, or ``None`` when the range cannot be read."""
    try:
        return git.rev_count(repo=worktree, range_spec=f"HEAD..{target}")
    except (OSError, CommandFailedError, ValueError):
        return None
