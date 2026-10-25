"""Repair the stalled open PRs a tick lists: one repair per stall, or a recorded unresolved action."""

from collections.abc import Sequence
from dataclasses import dataclass

from teatree.core.intake.task_enqueue import enqueue_phase_task_once
from teatree.core.models.deferred_question import DeferredQuestion
from teatree.core.models.errors import NoPlanArtifactError
from teatree.core.models.plan_decision import plan_missing_refusal
from teatree.core.models.pull_request import PullRequest
from teatree.core.models.red_mr_fix_attempt import RedMrFixAttempt
from teatree.core.models.task import Task
from teatree.core.models.task_enqueue import TaskEnqueueError, phase_eligible
from teatree.core.models.ticket import Ticket
from teatree.core.review.pr_inventory import OpenPrRow
from teatree.core.review.pr_next_action import FIX_CI

_REPAIR_PHASE = "coding"
_IN_FLIGHT = (Task.Status.PENDING, Task.Status.CLAIMED)
_MARKER = "pr-stall:{url}:{cause}"


@dataclass(frozen=True, slots=True)
class StallRepair:
    ref: str
    cause: str
    action: str


def repair_stalled_prs(rows: Sequence[OpenPrRow]) -> list[StallRepair]:
    stalled = [row for row in rows if row.stall]
    owners = {
        pr.url: pr.ticket
        for pr in PullRequest.objects.filter(
            url__in=[row.url for row in stalled], ticket__role=Ticket.Role.AUTHOR
        ).select_related("ticket")
    }
    return [StallRepair(row.ref, row.stall, _repair(row, owners.get(row.url))) for row in stalled]


def _repair(row: OpenPrRow, ticket: Ticket | None) -> str:
    if ticket is None:
        return "unresolved: no author ticket owns this PR"
    if (running := ticket.tasks.filter(status__in=_IN_FLIGHT).order_by("pk").first()) is not None:
        return f"in flight: TODO-{running.pk} {running.phase}"
    if refusal := _refusal(ticket):
        _record_unresolved(row, refusal)
        return f"unresolved: {refusal}"
    if not row.head_sha:
        _record_unresolved(row, "head SHA is unknown")
        return "unresolved: head SHA is unknown"
    claim = RedMrFixAttempt.claim(pr_url=row.url, head_sha=row.head_sha, kind=_fix_kind(row), overlay=ticket.overlay)
    if claim is None:
        return "already attempted on this head"
    return _queue_repair(row, ticket, claim)


def _fix_kind(row: OpenPrRow) -> RedMrFixAttempt.Kind:
    if row.stall == FIX_CI:
        return RedMrFixAttempt.Kind.CI_RED
    if row.decision == "flag_conflict":
        return RedMrFixAttempt.Kind.MERGE_CONFLICT
    return RedMrFixAttempt.Kind.REVIEW_FINDINGS


def _queue_repair(row: OpenPrRow, ticket: Ticket, claim: RedMrFixAttempt) -> str:
    reason = f"Repair the stalled PR {row.ref}, open for {row.age}: {row.stall}."
    try:
        task = enqueue_phase_task_once(ticket=ticket, phase=_REPAIR_PHASE, reason=reason, agent_id="pr-stall")
    except (TaskEnqueueError, NoPlanArtifactError) as exc:
        claim.delete()
        _record_unresolved(row, str(exc))
        return f"unresolved: {exc}"
    return f"queued TODO-{task.pk} {task.phase}"


def _refusal(ticket: Ticket) -> str:
    if missing_plan := plan_missing_refusal(ticket, phase=_REPAIR_PHASE):
        return missing_plan
    return "" if phase_eligible(ticket, _REPAIR_PHASE) else f"{_REPAIR_PHASE} is not available from {ticket.state}"


def _record_unresolved(row: OpenPrRow, refusal: str) -> None:
    DeferredQuestion.record(
        f"PR {row.ref} is stalled ({row.stall}) and no safe automatic repair applies: {refusal} How should it proceed?",
        dedupe_marker=_MARKER.format(url=row.url, cause=row.stall),
    )
