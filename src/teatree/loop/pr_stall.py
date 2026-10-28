"""Repair the stalled open PRs a tick lists: one repair per stall, or a recorded unresolved action."""

from collections.abc import Sequence
from dataclasses import dataclass

from django.db import transaction

from teatree.core.models.deferred_question import DeferredQuestion
from teatree.core.models.pull_request import PullRequest
from teatree.core.models.red_mr_fix_attempt import RedMrFixAttempt
from teatree.core.models.task import Task
from teatree.core.models.ticket import Ticket
from teatree.core.review.pr_inventory import OpenPrRow
from teatree.core.review.pr_next_action import FIX_CI

_REPAIR_PHASE = "debugging"
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
    if not ticket.admits_implementing():
        refusal = "no plan decision is available for corrective work"
        _record_unresolved(row, refusal)
        return f"unresolved: {refusal}"
    if not row.head_sha:
        _record_unresolved(row, "head SHA is unknown")
        return "unresolved: head SHA is unknown"
    with transaction.atomic():
        claim = RedMrFixAttempt.claim(
            pr_url=row.url, head_sha=row.head_sha, kind=_fix_kind(row), overlay=ticket.overlay
        )
        if claim is None:
            return "already attempted on this head"
        task = ticket.schedule_implementing(
            _REPAIR_PHASE,
            reason=f"Repair the stalled PR {row.ref}, open for {row.age}: {row.stall}.",
        )
    return f"queued TODO-{task.pk} {task.phase}"


def _fix_kind(row: OpenPrRow) -> RedMrFixAttempt.Kind:
    if row.stall == FIX_CI:
        return RedMrFixAttempt.Kind.CI_RED
    if row.decision == "flag_conflict":
        return RedMrFixAttempt.Kind.MERGE_CONFLICT
    return RedMrFixAttempt.Kind.REVIEW_FINDINGS


def _record_unresolved(row: OpenPrRow, refusal: str) -> None:
    DeferredQuestion.record(
        f"PR {row.ref} is stalled ({row.stall}) and no safe automatic repair applies: {refusal} How should it proceed?",
        dedupe_marker=_MARKER.format(url=row.url, cause=row.stall),
    )
