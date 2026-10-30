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
from pathlib import Path
from typing import TYPE_CHECKING

from teatree.core.gates.merge_item_waiver import waived_merge_items
from teatree.core.merge.ticket_resolution import gated_ticket_for_review_task
from teatree.core.modelkit.phases import normalize_phase
from teatree.core.models.critic_verdict import CriticVerdict
from teatree.core.models.plan_adequacy import declared_seam_paths
from teatree.core.models.plan_artifact import PlanArtifact
from teatree.core.models.pull_request import PullRequest
from teatree.core.models.red_mr_fix_attempt import RedMrFixAttempt
from teatree.core.models.review_hold_disposition import ReviewHoldDisposition
from teatree.core.models.review_target import review_target_for_task
from teatree.core.models.review_verdict import ReviewVerdict
from teatree.core.models.rubric import Rubric
from teatree.core.models.ticket_worktree_checks import dispatch_worktree_path
from teatree.core.review.finding_standard import FINDING_STANDARD
from teatree.core.review.review_delta import BRIEF_PATCH_CHARS
from teatree.core.worktree.branch_currency import fetch_target_head, predict_merge_conflicts
from teatree.core.worktree.target_branch import resolve_target_branch
from teatree.quality.review_evidence import ReviewEvidenceError, read_review_evidence
from teatree.utils import git
from teatree.utils.run import CommandFailedError
from teatree.utils.url_slug import pr_ref_from_url

if TYPE_CHECKING:
    from teatree.core.models.task import Task
    from teatree.core.review.review_plan import ReviewPlan

# A committer-timestamp / subject record from one `git log -1`, unit-separated so
# a subject containing spaces or the field text is never mis-split.
_UNIT = "\x1f"


def standing_hold_brief_lines(task: "Task") -> tuple[str, ...]:
    target = review_target_for_task(task)
    if target is None or not target.head_sha:
        return ()
    holds = ReviewVerdict.objects.unreconciled_holds_at(slug=target.slug, pr_id=target.pr_id, head_sha=target.head_sha)
    if not holds:
        return ()
    lines = [
        (
            "A later independent merge_safe verdict must answer EVERY standing HOLD point in "
            "review_verdict.hold_dispositions: [{hold_id, points: [{index, classification: "
            "disproved_premise|venue_limit, citation, counter_reproduction: {command, observed_result}}]}]. "
            "Cite fresh evidence for each point. A point marked [needs counter_reproduction] reports an "
            "executed failure: answer it only with the command or test node id you ran at this head and "
            "the result it printed; a citation alone leaves that point unanswered."
        )
    ]
    for hold in holds:
        lines.append(
            f"HOLD {hold.pk} by {hold.reviewer_identity}: indices "
            f"{sorted(ReviewHoldDisposition.blocking_indices(hold))}"
        )
        lines.extend(
            f"  {index}: {finding.describe()}"
            + (" [needs counter_reproduction]" if ReviewHoldDisposition.needs_counter_reproduction(finding) else "")
            for index, finding in enumerate(hold.structured_findings)
        )
    return tuple(lines)


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


def round_brief_lines(task: "Task") -> tuple[str, ...]:
    previous = (
        CriticVerdict.objects.filter(ticket=task.ticket, transition="merge").order_by("-recorded_at", "-pk").first()
    )
    findings = (
        tuple(
            f"  - {item.slug}: {item.status}; {item.citation}; "
            + (
                "; ".join(f"{point.id} {point.kind} {point.file}:{point.line} {point.summary}" for point in item.points)
                or (f"{previous.head_sha}:{item.slug}:0" if item.is_fail() else "no findings")
            )
            for item in previous.item_verdicts()
            if item.slug not in waived_merge_items(task.ticket)
        )
        if previous is not None
        else ("  - none recorded; inspect every merge-quality class on the first pass",)
    )
    state = resolve_head_state(task)
    review_findings = _open_review_findings_lines(task, state)
    worktree = dispatch_worktree_path(task.ticket)
    try:
        evidence = read_review_evidence(Path(worktree), state.sha) if state is not None and worktree else None
        measured = evidence.render() if evidence is not None else "UNAVAILABLE: no head-bound review evidence receipt."
    except ReviewEvidenceError as exc:
        measured = f"UNAVAILABLE: {exc}"
    command = (
        f"t3 tool review-evidence --expect-sha {state.sha}"
        if state is not None
        else "t3 tool review-evidence --expect-sha <committed-head>"
    )
    return (
        "",
        f"Finding standard: {FINDING_STANDARD}",
        (
            "Fix BLOCKING points in this round. A NIT is fixed only when it is a one-line edit on a line "
            "already changed for a BLOCKING point."
        ),
        "Prior merge critic findings:",
        *findings,
        measured,
        f"Optional measurement: `{command}` on a clean committed head; a missing receipt is report-only.",
        "The coder report must show the matrix result for each new or changed test.",
        *review_findings,
    )


def _open_review_findings_lines(task: "Task", state: HeadState | None) -> tuple[str, ...]:
    if normalize_phase(task.phase) != "debugging" or "review-findings fix" not in task.execution_reason:
        return ()
    urls = PullRequest.objects.live().filter(ticket=task.ticket).values_list("url", flat=True)
    rows = RedMrFixAttempt.objects.filter(pr_url__in=urls, kind=RedMrFixAttempt.Kind.REVIEW_FINDINGS).order_by(
        "-dispatched_at", "-pk"
    )
    row = next(
        (
            candidate
            for candidate in rows
            if candidate.pr_url in task.execution_reason
            and candidate.review_head_sha
            and (state is None or candidate.review_head_sha == state.sha)
        ),
        None,
    )
    if row is None or (ref := pr_ref_from_url(row.pr_url)) is None:
        return ()
    reviewed_sha = row.review_head_sha
    findings = [
        f"  - {verdict.reviewer_identity}: {finding.describe()}"
        for verdict in ReviewVerdict.objects.speaking_verdicts_at(slug=ref.slug, pr_id=ref.pr_id, head_sha=reviewed_sha)
        if verdict.stands_as_hold()
        for finding in verdict.structured_findings
        if finding.kind == "blocking"
    ]
    return ("", f"OPEN REVIEW FINDINGS at {reviewed_sha}:", *(findings or ["  (none recorded)"]))


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


def rubric_brief_lines(task: "Task", *, plan: "ReviewPlan | None" = None) -> tuple[str, ...]:
    """Render the reviewed ticket's rubric criteria for a reviewing brief, or ``()``.

    The reviewer is told to grade EVERY criterion and nothing else grades one, so a
    brief that names no criteria asks for a checklist the reviewer cannot see. Resolved
    through the SAME :func:`~teatree.core.merge.ticket_resolution.gated_ticket_for_review_task`
    the recorder stamps and the merge gate reads — a brief listing a rubric those two
    would not consult teaches the wrong checklist.

    ``()`` for a PR no ticket owns and for a ticket with no rubric, so a dispatch is
    byte-identical to today until a reviewed ticket actually carries criteria.
    """
    ticket = gated_ticket_for_review_task(task)
    if ticket is None:
        return ()
    rubric = Rubric.objects.active_for_ticket(ticket)
    if rubric is None:
        return ()
    criteria = list(rubric.criteria.all())
    if not criteria:
        return ()
    header = (
        f"TICKET RUBRIC (ticket {ticket.pk}) — grade EVERY one in `rubric_grades` "
        'as {"ordinal": <int>, "text_hash": "<hash>", "status": "pass"|"fail", "rationale": "<proof>"}:'
    )
    return (
        "",
        header,
        *(
            f"  #{c.ordinal} {c.text} [text_hash {c.text_hash}]"
            + (
                " CARRIED (return a new grade if the delta affects it)"
                if plan is not None and c.ordinal in plan.carried
                else ""
            )
            for c in criteria
        ),
    )


_MAX_REVIEW_DIFF_CHARS = BRIEF_PATCH_CHARS


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


_NO_MERGE_NEEDED = (
    "  No merge needed: the squash applies this diff onto the target and the live required checks gate the merge;"
    " a merge here would only move the head and restart CI."
)


def _merge_directive(target: str) -> tuple[str, ...]:
    return (
        "  Merge it in BEFORE gating — `t3 tool verify-gates` judges this tree against itself, never",
        f"  whether it still merges. Run `git merge --no-edit {target}` (merge, NEVER rebase), re-run",
        "  the targeted tests and `t3 tool verify-gates` on the MERGED tree, then commit the merge.",
    )


def _behind_lines(currency: "BranchCurrency") -> tuple[str, ...]:
    behind = f"  HEAD {currency.short_sha} is {currency.behind_count} commit(s) behind {currency.target}"
    if currency.conflicting_paths is None:
        return (f"{behind}; conflict prediction unavailable.", _UNVERIFIED_HINT, *_merge_directive(currency.target))
    if not currency.conflicting_paths:
        return (f"{behind} and merges clean.", _NO_MERGE_NEEDED)
    return (
        f"{behind} and CONFLICTS on: {', '.join(currency.conflicting_paths)}",
        *_merge_directive(currency.target),
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
