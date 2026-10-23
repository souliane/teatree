"""Carry prior merge judgments and the changed-line delta into a new critic claim."""

from difflib import unified_diff
from pathlib import Path
from typing import TYPE_CHECKING

from teatree.core.models.critic_verdict import CriticVerdict
from teatree.core.models.ticket_worktree_checks import dispatch_worktree_path
from teatree.core.worktree.target_branch import resolve_target_branch
from teatree.quality.review_evidence import ReviewEvidenceError, read_review_evidence
from teatree.utils import git
from teatree.utils.run import CommandFailedError

if TYPE_CHECKING:
    from teatree.core.models.ticket import Ticket


_MAX_DELTA_CHARS = 24000


def review_evidence_contract(ticket: "Ticket", head_sha: str) -> str:
    worktree = dispatch_worktree_path(ticket)
    if not worktree:
        return "DETERMINISTIC EVIDENCE REPORT ONLY: ticket worktree missing; judge test_value from the PR diff."
    try:
        evidence = read_review_evidence(Path(worktree), head_sha)
    except ReviewEvidenceError as exc:
        return f"DETERMINISTIC EVIDENCE REPORT ONLY: {exc}; judge test_value from the PR diff."
    if evidence is None:
        return "DETERMINISTIC EVIDENCE REPORT ONLY: no receipt for this head; judge test_value from the PR diff."
    if any(not row.measured for row in evidence.matrix.rows):
        return f"DETERMINISTIC EVIDENCE REPORT ONLY: measurement incomplete.\n{evidence.render()}"
    return f"DETERMINISTIC EVIDENCE AT {head_sha}:\n{evidence.render()}"


def merge_critic_continuity(ticket: "Ticket", head_sha: str) -> str:
    previous = (
        CriticVerdict.objects.filter(ticket=ticket, transition="merge")
        .exclude(head_sha=head_sha)
        .order_by("-recorded_at", "-pk")
        .first()
    )
    if previous is None:
        return (
            "FIRST PASS: inspect the complete PR diff and every changed file. Judge every merge item "
            "exhaustively; do not infer a pass from tests alone."
        )
    prior = "\n".join(
        f"- {item.slug}: {item.status}; {item.citation}; points: "
        + "; ".join(f"{point.id} {point.kind} {point.file}:{point.line} {point.summary}" for point in item.points)
        for item in previous.item_verdicts()
    )
    worktree = dispatch_worktree_path(ticket)
    if not worktree:
        delta = "UNAVAILABLE: the ticket worktree is missing; return unverified, not pass or fail."
    else:
        try:
            base_ref = resolve_target_branch(ticket, worktree)
            old_pr_diff = git.run_strict(
                repo=worktree, args=["diff", "--unified=0", f"{base_ref}...{previous.head_sha}", "--"]
            )
            new_pr_diff = git.run_strict(repo=worktree, args=["diff", "--unified=0", f"{base_ref}...{head_sha}", "--"])
            delta = "\n".join(
                unified_diff(
                    old_pr_diff.splitlines(),
                    new_pr_diff.splitlines(),
                    fromfile="prior PR diff",
                    tofile="current PR diff",
                    lineterm="",
                )
            )
        except (OSError, CommandFailedError):
            delta = "UNAVAILABLE: the prior-to-current diff could not be read; return unverified, not pass or fail."
    if len(delta) > _MAX_DELTA_CHARS:
        delta = "UNAVAILABLE: the prior-to-current diff exceeds the critic evidence limit; return unverified."
    return (
        f"CONTINUITY FROM {previous.head_sha}: recheck each previous finding and state fixed or not fixed "
        "with a citation. Evaluate new concerns only on the changed lines shown below. Preserve prior "
        "passes for unchanged lines; a formerly passed item can fail only for a changed-line defect. "
        "An unavailable delta leaves every item unverified.\n"
        f"Prior judgment:\n{prior}\nChanged-line delta to {head_sha}:\n{delta or '(no changed lines)'}"
    )
