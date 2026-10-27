"""Persist the review case once per ticket and head for briefs and verdict recording."""

import re
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

from teatree.core.merge.ticket_resolution import gated_ticket_for_review_task
from teatree.core.models.head_carry_over import HeadCarryOver
from teatree.core.models.review_target import ReviewTarget, review_target_for_task
from teatree.core.models.review_verdict import ReviewVerdict
from teatree.core.models.reviewer_identity import is_independent_reviewer_identity
from teatree.core.models.round_metric import RoundMetric
from teatree.core.models.rubric import Rubric
from teatree.core.models.task_attempt import TaskAttempt
from teatree.core.models.ticket_worktree_checks import dispatch_worktree_path
from teatree.core.review.live_head import dispatch_review_target
from teatree.core.review.review_delta import BRIEF_PATCH_CHARS, ReviewDelta, resolve_delta
from teatree.core.worktree.clone_paths import matching_clone
from teatree.core.worktree.target_branch import resolve_target_branch

if TYPE_CHECKING:
    from teatree.core.models.task import Task
    from teatree.core.models.ticket import Ticket

_PATH = re.compile(
    r"(?<![\w./-])(?:[\w.-]+/[\w./-]+|[\w.-]+\.(?:py|js|ts|tsx|jsx|go|rs|java|kt|swift|rb|php|c|cc|cpp|h|hpp|sql|sh|md|json|yaml|yml|toml|cfg|ini|html|css|scss|txt))\b"
)


@dataclass(frozen=True, slots=True)
class ReviewPlan:
    head: str
    delta: ReviewDelta
    carried: frozenset[int]
    prior_findings: tuple[str, ...]
    prior_decisions: tuple[str, ...]
    prior_grades: tuple[str, ...]
    all_ordinals: frozenset[int] = frozenset()

    def valid_carried(self, task: "Task", head: str) -> frozenset[int]:
        if self.head != head or self.delta.case != "delta":
            return frozenset()
        target = review_target_for_task(task)
        if (
            target is None
            or not ReviewVerdict.objects.for_pr(target.slug, target.pr_id)
            .filter(reviewed_sha=self.delta.prior_head)
            .exists()
        ):
            return frozenset()
        return self.carried & _carry_ordinals(task, self.delta.prior_head, self.delta.files)

    def brief_lines(self, *, include_patch: bool = True) -> tuple[str, ...]:
        case = (
            f"REVIEW CASE: delta since {self.delta.prior_head[:12]}"
            if self.delta.case == "delta"
            else f"REVIEW CASE: full - {self.delta.reason}"
        )
        lines = ["", case]
        if self.delta.prior_head:
            lines.extend(
                (
                    f"Prior reviewed head: {self.delta.prior_head}",
                    "Prior findings:",
                    *(self.prior_findings or ("  (none)",)),
                    "Prior decisions:",
                    *(self.prior_decisions or ("  (none)",)),
                    "Prior rubric grades:",
                    *(self.prior_grades or ("  (none)",)),
                    "Do not reopen settled calls without new evidence; restate every unfixed prior finding.",
                )
            )
        if self.delta.case == "delta":
            lines.extend(
                (
                    "CARRIED: " + (", ".join(f"#{n}" for n in sorted(self.carried)) or "none"),
                    "RE-GRADE: " + (", ".join(f"#{n}" for n in sorted(self.all_ordinals - self.carried)) or "none"),
                    "Re-grade any carried row the delta can affect.",
                    "Run only tests the delta can reach; CI owns the full-tree run.",
                )
            )
            if include_patch:
                lines.extend(
                    (
                        "DIFF UNDER REVIEW (PR-side commits since the prior reviewed head):",
                        "```diff",
                        self.delta.patch[:BRIEF_PATCH_CHARS]
                        + (
                            "\n… (diff truncated — Read the changed files directly)"
                            if len(self.delta.patch) > BRIEF_PATCH_CHARS
                            else ""
                        ),
                        "```",
                    )
                )
        return tuple(lines)


def _carry_ordinals(task: "Task", prior: str, files: frozenset[str]) -> frozenset[int]:
    ticket = gated_ticket_for_review_task(task) or task.ticket
    rubric = Rubric.objects.active_for_ticket(ticket)
    if rubric is None:
        return frozenset()
    by_head = (type(ticket).objects.get(pk=ticket.pk).extra or {}).get("rubric_grades_by_head", {})
    grades = by_head.get(prior, {}) if isinstance(by_head, dict) else {}
    if not isinstance(grades, dict):
        return frozenset()
    carried: set[int] = set()
    changed_names = {Path(path).name for path in files}
    for criterion in rubric.criteria.all():
        grade = grades.get(str(criterion.pk))
        if not isinstance(grade, dict) or str(grade.get("status", "")).lower() != "pass":
            continue
        if not is_independent_reviewer_identity(str(grade.get("grader_identity", ""))):
            continue
        cited = set(_PATH.findall(str(grade.get("rationale", ""))))
        if cited and changed_names.isdisjoint(Path(path).name for path in cited):
            carried.add(criterion.ordinal)
    return frozenset(carried)


def _prior_lines(task: "Task", prior: str) -> tuple[tuple[str, ...], tuple[str, ...], tuple[str, ...]]:
    target = review_target_for_task(task)
    if target is None or not prior:
        return (), (), ()
    rows = list(ReviewVerdict.objects.for_pr(target.slug, target.pr_id).filter(reviewed_sha=prior))
    findings = tuple(
        f"  {row.reviewer_identity}: {item.get('kind', 'blocking')} "
        f"{item.get('file', '')}:{item.get('line', 0)} {item.get('summary', '')}"
        for row in rows
        for item in row.findings
        if isinstance(item, dict)
    )
    attempt = (
        TaskAttempt.objects.filter(task__ticket=task.ticket, result__review_verdict__reviewed_sha=prior)
        .order_by("-pk")
        .first()
    )
    result = attempt.result if attempt is not None and isinstance(attempt.result, dict) else {}
    decisions = tuple(f"  {value}" for value in result.get("decisions", []) if isinstance(value, str))
    ticket = gated_ticket_for_review_task(task) or task.ticket
    by_head = (type(ticket).objects.get(pk=ticket.pk).extra or {}).get("rubric_grades_by_head", {})
    grades = by_head.get(prior, {}) if isinstance(by_head, dict) else {}
    rubric = Rubric.objects.active_for_ticket(ticket)
    grade_lines = (
        tuple(
            f"  #{criterion.ordinal} {grade.get('status', '')} by "
            f"{grade.get('grader_identity', '')}: {grade.get('rationale', '')}"
            for criterion in rubric.criteria.all()
            if isinstance(grade := grades.get(str(criterion.pk)), dict)
        )
        if rubric is not None and isinstance(grades, dict)
        else ()
    )
    return findings, decisions, grade_lines


def _open_plan(task: "Task", ticket: "Ticket", target: ReviewTarget, head: str, repo: Path | str | None) -> RoundMetric:
    repo = repo or dispatch_worktree_path(ticket) or matching_clone(target.slug)
    carried_heads = {
        move.to_head: move
        for move in HeadCarryOver.objects.filter(
            slug__iexact=target.slug, pr_id=target.pr_id, outcome=HeadCarryOver.Outcome.CARRIED
        )
        if move.reviewed_by_carry
    }
    prior_heads = list(
        dict.fromkeys(
            row.reviewed_sha
            for row in ReviewVerdict.objects.for_pr(target.slug, target.pr_id)
            .exclude(reviewed_sha=head)
            .order_by("-recorded_at", "-pk")
            if row.reviewed_sha not in carried_heads or row.recorded_at > carried_heads[row.reviewed_sha].recorded_at
        )
    )
    previous = RoundMetric.objects.filter(kind=RoundMetric.Kind.REVIEW, ticket=ticket).order_by("-round_number").first()
    base = resolve_target_branch(ticket, str(repo)) if repo is not None else ""
    prior_base = str(previous.detail.get("base_ref", "")) if previous is not None else ""
    delta = (
        resolve_delta(Path(repo), prior_heads, head, base, prior_base)
        if repo is not None
        else ReviewDelta("full", "git clone unavailable", prior_head=prior_heads[0] if prior_heads else "")
    )
    carried = _carry_ordinals(task, delta.prior_head, delta.files) if delta.case == "delta" else frozenset()
    return RoundMetric.objects.open(
        kind=RoundMetric.Kind.REVIEW,
        ticket=ticket,
        subject=head,
        size=delta.changed_lines if delta.case == "delta" else 0,
        detail={
            "case": delta.case,
            "reason": delta.reason,
            "prior_head": delta.prior_head,
            "patch": delta.patch[: BRIEF_PATCH_CHARS + 1],
            "delta_files": sorted(delta.files),
            "pr_files": sorted(delta.pr_files),
            "base_ref": base,
            "carried": sorted(carried),
        },
    )


def plan_for_task(task: "Task", *, create: bool = True) -> ReviewPlan | None:
    target = dispatch_review_target(task)
    if target is None:
        return None
    ticket = gated_ticket_for_review_task(task) or task.ticket
    head = target.head_sha.strip().lower()
    if not head:
        return None
    repo = None
    row = RoundMetric.objects.filter(kind=RoundMetric.Kind.REVIEW, ticket=ticket, subject=head).first()
    if row is None:
        if not create:
            return None
        row = _open_plan(task, ticket, target, head, repo)
    detail = row.detail
    delta = ReviewDelta(
        str(detail["case"]),
        str(detail.get("reason", "")),
        str(detail.get("prior_head", "")),
        str(detail.get("patch", "")),
        frozenset(detail.get("delta_files", [])),
        frozenset(detail.get("pr_files", [])),
    )
    prior_findings, prior_decisions, prior_grades = _prior_lines(task, delta.prior_head)
    rubric = Rubric.objects.active_for_ticket(ticket)
    ordinals = frozenset(rubric.criteria.values_list("ordinal", flat=True)) if rubric is not None else frozenset()
    return ReviewPlan(
        head,
        delta,
        frozenset(detail.get("carried", [])) & ordinals,
        prior_findings,
        prior_decisions,
        prior_grades,
        ordinals,
    )
