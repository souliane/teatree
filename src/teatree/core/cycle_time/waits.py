"""Attributed time from a ticket's first task admission to its first merge."""

from collections import defaultdict
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta
from itertools import pairwise
from operator import itemgetter
from typing import TypedDict

from django.db.models import Q
from django.utils import timezone

from teatree.core.cycle_time.spans import PhaseSpan, spans_for_tickets
from teatree.core.cycle_time.timeline import engagements_for_tickets
from teatree.core.cycle_time.work_intervals import Engagement, merged_intervals, subtract_intervals
from teatree.core.models.deferred_question import DeferredQuestion
from teatree.core.models.pull_request import PullRequest
from teatree.core.models.task import Task
from teatree.core.models.task_attempt import TaskAttempt
from teatree.core.models.task_cancel_source import count_recorded_cancels
from teatree.core.models.ticket import Ticket
from teatree.core.models.transition import TicketTransition
from teatree.core.models.usage_window_state import (
    CHECKOUT_PARKED_PREFIX,
    LIMIT_PARKED_PREFIX,
    PARK_ATTEMPT_RETENTION_DAYS,
)
from teatree.core.models.worktree_occupancy import task_holder_id

WAIT_CAUSES = ("free_seat", "checkout_fence", "sibling_task", "quota", "human", "no_next_task", "review")
WAIT_LABELS = {
    "free_seat": "No free seat",
    "checkout_fence": "Checkout fence",
    "sibling_task": "Sibling task holds checkout",
    "quota": "Quota lane",
    "human": "Waiting for a human",
    "no_next_task": "No next task",
    "review": "Review",
}
_WAIT_PRECEDENCE = ("human", "sibling_task", "checkout_fence", "quota", "free_seat", "review")


@dataclass(frozen=True, slots=True)
class WaitBlock:
    cause: str
    start: datetime
    end: datetime

    @property
    def seconds(self) -> float:
        return (self.end - self.start).total_seconds()


@dataclass(frozen=True, slots=True)
class MergeBreakdown:
    ticket_id: int
    number: str
    pr_url: str
    started_at: datetime | None
    merged_at: datetime
    elapsed_seconds: float
    work_seconds: float
    wait_seconds: dict[str, float]
    longest_wait: WaitBlock | None
    hand_cancels: int
    measured: bool
    parks_retained: bool


class _WaitAttemptResult(TypedDict, total=False):
    checkout_holder: str
    cancel_source: str


@dataclass(frozen=True, slots=True)
class _MergeInputs:
    pr_url: str
    tasks: Sequence[Task]
    attempts: dict[int, list[tuple[datetime, datetime | None, str, _WaitAttemptResult]]]
    questions: Sequence[tuple[datetime, datetime | None]]
    review_spans: Sequence[PhaseSpan]
    engagements: Sequence[Engagement]


def recent_merge_breakdowns(limit: int = 10) -> tuple[MergeBreakdown, ...]:
    merges = TicketTransition.objects.recent_merges(limit)
    if not merges:
        return ()
    ticket_ids = [ticket_id for ticket_id, _merged_at in merges]
    tickets = Ticket.objects.in_bulk(ticket_ids)
    tasks: dict[int, list[Task]] = defaultdict(list)
    for task in Task.objects.filter(ticket_id__in=ticket_ids).only(
        "id", "ticket_id", "created_at", "admitted_at", "status"
    ):
        tasks[task.ticket_id].append(task)
    attempts: dict[int, list[tuple[datetime, datetime | None, str, _WaitAttemptResult]]] = defaultdict(list)
    for task_id, started_at, ended_at, error, result in TaskAttempt.objects.filter(
        task__ticket_id__in=ticket_ids
    ).values_list("task_id", "started_at", "ended_at", "error", "result"):
        attempts[task_id].append((started_at, ended_at, error, _wait_attempt_result(result)))
    questions: dict[int, list[tuple[datetime, datetime | None]]] = defaultdict(list)
    for parked_ticket_id, session_ticket_id, created, answered, dismissed in DeferredQuestion.objects.filter(
        Q(parked_task__ticket_id__in=ticket_ids) | Q(task_session__ticket_id__in=ticket_ids)
    ).values_list("parked_task__ticket_id", "task_session__ticket_id", "created_at", "answered_at", "dismissed_at"):
        questions[parked_ticket_id or session_ticket_id].append((created, answered or dismissed))
    spans = spans_for_tickets(ticket_ids)
    engagements = engagements_for_tickets(ticket_ids)
    urls = dict(
        PullRequest.objects.filter(ticket_id__in=ticket_ids, state=PullRequest.State.MERGED)
        .order_by("pk")
        .values_list("ticket_id", "url")
    )
    cutoff = timezone.now() - timedelta(days=PARK_ATTEMPT_RETENTION_DAYS)
    return tuple(
        _breakdown(
            tickets[ticket_id],
            merged_at,
            _MergeInputs(
                pr_url=urls.get(ticket_id, ""),
                tasks=tasks.get(ticket_id, ()),
                attempts=attempts,
                questions=questions.get(ticket_id, ()),
                review_spans=[
                    span
                    for span in spans.get(ticket_id, ())
                    if span.from_state in {Ticket.State.PR_OPENED, Ticket.State.REVIEW_REQUESTED}
                ],
                engagements=[
                    engagement
                    for (each_id, _phase), rows in engagements.items()
                    if each_id == ticket_id
                    for engagement in rows
                ],
            ),
            cutoff,
        )
        for ticket_id, merged_at in merges
        if ticket_id in tickets
    )


def _breakdown(
    ticket: Ticket,
    merged_at: datetime,
    inputs: _MergeInputs,
    cutoff: datetime,
) -> MergeBreakdown:
    start = min((task.admitted_at for task in inputs.tasks if task.admitted_at is not None), default=None)
    cancels = count_recorded_cancels(
        (
            (ended, result)
            for task in inputs.tasks
            for _created, ended, _error, result in inputs.attempts.get(task.pk, ())
        ),
        start=start,
        merged_at=merged_at,
    )
    waits = dict.fromkeys(WAIT_CAUSES, 0.0)
    if start is None or start >= merged_at or _readmission_obscures_work(inputs, merged_at):
        return MergeBreakdown(
            ticket_id=ticket.pk,
            number=ticket.ticket_number,
            pr_url=inputs.pr_url,
            started_at=start,
            merged_at=merged_at,
            elapsed_seconds=0.0,
            work_seconds=0.0,
            wait_seconds=waits,
            longest_wait=None,
            hand_cancels=cancels,
            measured=False,
            parks_retained=False,
        )

    tagged: list[tuple[datetime, datetime, str]] = []
    park_windows: dict[int, list[tuple[datetime, datetime]]] = defaultdict(list)
    for task in inputs.tasks:
        task_tags, task_windows = _task_wait_tags(task, inputs, merged_at)
        tagged.extend(task_tags)
        park_windows[task.pk].extend(task_windows)
    tagged.extend((created, ended or merged_at, "human") for created, ended in inputs.questions)
    tagged.extend((span.entered_at, span.left_at, "review") for span in inputs.review_spans)
    tagged = [(max(start, lower), min(merged_at, upper), cause) for lower, upper, cause in tagged if lower < upper]
    tagged = [(lower, upper, cause) for lower, upper, cause in tagged if lower < upper]
    work = merged_intervals(
        (
            Engagement(lower, upper)
            for engagement in inputs.engagements
            for lower, upper in subtract_intervals(
                ((engagement.start, engagement.end or merged_at),), park_windows.get(engagement.task_id, ())
            )
        ),
        start=start,
        end=merged_at,
    )
    work_seconds, waits, longest = _split_work_and_waits(start, merged_at, tagged, work)
    return MergeBreakdown(
        ticket_id=ticket.pk,
        number=ticket.ticket_number,
        pr_url=inputs.pr_url,
        started_at=start,
        merged_at=merged_at,
        elapsed_seconds=(merged_at - start).total_seconds(),
        work_seconds=work_seconds,
        wait_seconds=waits,
        longest_wait=longest,
        hand_cancels=cancels,
        measured=True,
        parks_retained=start >= cutoff,
    )


def _split_work_and_waits(
    start: datetime,
    merged_at: datetime,
    tagged: Sequence[tuple[datetime, datetime, str]],
    work: Sequence[tuple[datetime, datetime]],
) -> tuple[float, dict[str, float], WaitBlock | None]:
    boundaries = sorted(
        {
            start,
            merged_at,
            *(point for lower, upper, _cause in tagged for point in (lower, upper)),
            *(point for lower, upper in work for point in (lower, upper)),
        }
    )
    waits = dict.fromkeys(WAIT_CAUSES, 0.0)
    blocks: list[WaitBlock] = []
    work_seconds = 0.0
    for lower, upper in pairwise(boundaries):
        seconds = (upper - lower).total_seconds()
        if any(work_start <= lower < work_end for work_start, work_end in work):
            work_seconds += seconds
            continue
        cause = next(
            (name for name in _WAIT_PRECEDENCE if any(a <= lower < b and tag == name for a, b, tag in tagged)),
            "no_next_task",
        )
        waits[cause] += seconds
        if blocks and blocks[-1].cause == cause and blocks[-1].end == lower:
            blocks[-1] = WaitBlock(cause=cause, start=blocks[-1].start, end=upper)
        else:
            blocks.append(WaitBlock(cause=cause, start=lower, end=upper))
    longest = max(blocks, key=lambda block: block.seconds) if blocks else None
    return work_seconds, waits, longest


def _task_wait_tags(
    task: Task, inputs: _MergeInputs, merged_at: datetime
) -> tuple[list[tuple[datetime, datetime, str]], list[tuple[datetime, datetime]]]:
    tags = [(task.created_at, task.admitted_at or merged_at, "free_seat")] if task.created_at else []
    parks = sorted(
        (
            (created, error, result)
            for created, _ended, error, result in inputs.attempts.get(task.pk, ())
            if error.startswith((CHECKOUT_PARKED_PREFIX, LIMIT_PARKED_PREFIX))
        ),
        key=itemgetter(0),
    )
    sibling_holders = {task_holder_id(other) for other in inputs.tasks if other.pk != task.pk}
    windows: list[tuple[datetime, datetime]] = []
    for index, (created, error, result) in enumerate(parks):
        next_park = parks[index + 1][0] if index + 1 < len(parks) else merged_at
        next_admission = task.admitted_at if task.admitted_at and task.admitted_at > created else merged_at
        end = min(next_park, next_admission, merged_at)
        cause = "quota" if error.startswith(LIMIT_PARKED_PREFIX) else "checkout_fence"
        if cause == "checkout_fence" and result.get("checkout_holder") in sibling_holders:
            cause = "sibling_task"
        tags.append((created, end, cause))
        windows.append((created, end))
    return tags, windows


def _readmission_obscures_work(inputs: _MergeInputs, merged_at: datetime) -> bool:
    for task in inputs.tasks:
        attempts = inputs.attempts.get(task.pk, ())
        real_ends = [
            ended
            for _created, ended, error, result in attempts
            if ended is not None
            and ended <= merged_at
            and not error.startswith((CHECKOUT_PARKED_PREFIX, LIMIT_PARKED_PREFIX))
            and "cancel_source" not in result
        ]
        if not real_ends:
            continue
        admitted = task.admitted_at
        if admitted is None or any(ended < admitted for ended in real_ends):
            return True
        if any(
            parked < ended and admitted <= parked
            for parked, _ended, error, _result in attempts
            if error.startswith((CHECKOUT_PARKED_PREFIX, LIMIT_PARKED_PREFIX))
            for ended in real_ends
        ):
            return True
    return False


def _wait_attempt_result(raw: object) -> _WaitAttemptResult:
    result: _WaitAttemptResult = {}
    if not isinstance(raw, dict):
        return result
    holder = raw.get("checkout_holder")
    if isinstance(holder, str):
        result["checkout_holder"] = holder
    cancel = raw.get("cancel_source")
    if isinstance(cancel, str):
        result["cancel_source"] = cancel
    return result
