"""First admission to first merge is the one start-to-merged measure for #5309; ``no_next_task`` is the remainder."""

from collections import defaultdict
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta
from enum import StrEnum
from itertools import pairwise
from operator import attrgetter

from django.db.models import Q
from django.utils import timezone

from teatree.core.cycle_time.spans import PhaseSpan, spans_for_tickets
from teatree.core.cycle_time.timeline import engagements_for_tickets, ticket_attempts
from teatree.core.cycle_time.work_intervals import Engagement, merged_intervals, subtract_intervals
from teatree.core.modelkit.owner_action import ATTEST_MARKER_PREFIX
from teatree.core.modelkit.phases import normalize_phase
from teatree.core.models.deferred_question import DeferredQuestion
from teatree.core.models.pull_request import PullRequest
from teatree.core.models.session import Session
from teatree.core.models.task import Task
from teatree.core.models.task_cancel_source import CANCEL_SOURCE_KEY, count_recorded_cancels
from teatree.core.models.ticket import Ticket
from teatree.core.models.transition import TicketTransition
from teatree.core.models.usage_window_state import (
    LIMIT_PARKED_PREFIX,
    OPERATOR_PAUSED_PREFIX,
    PARK_ATTEMPT_RETENTION_DAYS,
    REVIEW_HELD_PREFIX,
)
from teatree.core.models.worktree_occupancy import task_holder_id


class WaitCause(StrEnum):
    FREE_SEAT = "free_seat"
    NOT_ADMITTED = "not_admitted"
    CHECKOUT_FENCE = "checkout_fence"
    SIBLING_TASK = "sibling_task"
    QUOTA = "quota"
    HUMAN = "human"
    NO_NEXT_TASK = "no_next_task"
    REVIEW = "review"


WAIT_CAUSES = tuple(WaitCause)
_PARK_CAUSES = {
    OPERATOR_PAUSED_PREFIX: WaitCause.HUMAN,
    LIMIT_PARKED_PREFIX: WaitCause.QUOTA,
    REVIEW_HELD_PREFIX: WaitCause.REVIEW,
}
WAIT_LABELS = {
    WaitCause.FREE_SEAT: "Waiting for a free seat",
    WaitCause.NOT_ADMITTED: "Not admitted, cause not recorded",
    WaitCause.CHECKOUT_FENCE: "Checkout fence",
    WaitCause.SIBLING_TASK: "Sibling task holds checkout",
    WaitCause.QUOTA: "Quota lane",
    WaitCause.HUMAN: "Waiting for a human",
    WaitCause.NO_NEXT_TASK: "No next task",
    WaitCause.REVIEW: "Review",
}
_WAIT_PRECEDENCE = (
    WaitCause.HUMAN,
    WaitCause.SIBLING_TASK,
    WaitCause.CHECKOUT_FENCE,
    WaitCause.QUOTA,
    WaitCause.FREE_SEAT,
    WaitCause.NOT_ADMITTED,
    WaitCause.REVIEW,
)

type _Tag = tuple[datetime, datetime, WaitCause]


@dataclass(frozen=True, slots=True)
class WaitBlock:
    cause: WaitCause
    start: datetime
    end: datetime

    @property
    def seconds(self) -> float:
        return (self.end - self.start).total_seconds()


@dataclass(frozen=True, slots=True)
class MergeWaitBreakdown:
    ticket_id: int
    number: str
    pr_url: str
    started_at: datetime | None
    merged_at: datetime
    elapsed_seconds: float
    work_seconds: float
    wait_seconds: dict[WaitCause, float]
    longest_wait: WaitBlock | None
    hand_cancels: int
    inferred_hand_tasks: int
    measured: bool
    parks_retained: bool


@dataclass(frozen=True, slots=True)
class _Attempt:
    started_at: datetime
    ended_at: datetime | None
    error: str
    holder: str | None
    cancel: str | None
    billed: bool
    parked: bool


@dataclass(frozen=True, slots=True)
class _MergeInputs:
    pr_url: str
    tasks: Sequence[Task]
    first_session_id: int | None
    attempts: dict[int, list[_Attempt]]
    questions: Sequence[tuple[datetime, datetime | None, WaitCause]]
    review_spans: Sequence[PhaseSpan]
    engagements: Sequence[Engagement]


def build_merge_wait_breakdowns(limit: int = 10) -> tuple[MergeWaitBreakdown, ...]:
    merges = TicketTransition.objects.recent_merges(limit)
    if not merges:
        return ()
    ticket_ids = [ticket_id for ticket_id, _merged_at in merges]
    tickets = Ticket.objects.in_bulk(ticket_ids)
    tasks: dict[int, list[Task]] = defaultdict(list)
    for task in Task.objects.filter(ticket_id__in=ticket_ids).only(
        "id", "ticket_id", "session_id", "parent_task_id", "phase", "created_at", "admitted_at", "status"
    ):
        tasks[task.ticket_id].append(task)
    attempts = _attempts_for_tickets(ticket_ids)
    first_sessions: dict[int, int] = {}
    for ticket_id, session_id in (
        Session.objects.filter(ticket_id__in=ticket_ids).order_by("pk").values_list("ticket_id", "pk")
    ):
        first_sessions.setdefault(ticket_id, session_id)
    questions: dict[int, list[tuple[datetime, datetime | None, WaitCause]]] = defaultdict(list)
    for parked_ticket_id, session_ticket_id, created, answered, dismissed, marker in DeferredQuestion.objects.filter(
        Q(parked_task__ticket_id__in=ticket_ids) | Q(task_session__ticket_id__in=ticket_ids)
    ).values_list(
        "parked_task__ticket_id",
        "task_session__ticket_id",
        "created_at",
        "answered_at",
        "dismissed_at",
        "dedupe_marker",
    ):
        attestation = marker.startswith(ATTEST_MARKER_PREFIX) and parked_ticket_id
        cause = WaitCause.CHECKOUT_FENCE if attestation else WaitCause.HUMAN
        questions[parked_ticket_id or session_ticket_id].append((created, answered or dismissed, cause))
    spans = spans_for_tickets(ticket_ids)
    engagements = engagements_for_tickets(ticket_ids)
    urls: dict[int, str] = {}
    for ticket_id, url in (
        PullRequest.objects.filter(ticket_id__in=ticket_ids, state=PullRequest.State.MERGED)
        .order_by("pk")
        .values_list("ticket_id", "url")
    ):
        urls.setdefault(ticket_id, url)
    cutoff = timezone.now() - timedelta(days=PARK_ATTEMPT_RETENTION_DAYS)
    return tuple(
        _breakdown(
            tickets[ticket_id],
            merged_at,
            _MergeInputs(
                pr_url=urls[ticket_id],
                tasks=tasks.get(ticket_id, ()),
                first_session_id=first_sessions.get(ticket_id),
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


def _attempts_for_tickets(ticket_ids: Sequence[int]) -> dict[int, list[_Attempt]]:
    attempts: dict[int, list[_Attempt]] = defaultdict(list)
    for task_id, *fields in ticket_attempts(ticket_ids).values_list(
        "task_id",
        "started_at",
        "ended_at",
        "error",
        "result__occupied_by__holder",
        f"result__{CANCEL_SOURCE_KEY}",
        "billed",
        "parked",
    ):
        started_at, ended_at, error, holder, cancel, billed, parked = fields
        attempts[task_id].append(
            _Attempt(
                started_at=started_at,
                ended_at=ended_at,
                error=error,
                holder=holder if isinstance(holder, str) else None,
                cancel=cancel if isinstance(cancel, str) else None,
                billed=billed,
                parked=parked,
            )
        )
    return attempts


def _breakdown(ticket: Ticket, merged_at: datetime, inputs: _MergeInputs, cutoff: datetime) -> MergeWaitBreakdown:
    start = min((task.admitted_at for task in inputs.tasks if task.admitted_at is not None), default=None)
    cancels = count_recorded_cancels(
        (
            (attempt.ended_at, {CANCEL_SOURCE_KEY: attempt.cancel})
            for task in inputs.tasks
            for attempt in inputs.attempts.get(task.pk, ())
        ),
        start=start,
        merged_at=merged_at,
    )
    inferred = _inferred_hand_tasks(inputs.tasks, inputs.first_session_id)
    if start is None or start >= merged_at or _readmission_obscures_work(inputs, merged_at, start):
        return MergeWaitBreakdown(
            ticket_id=ticket.pk,
            number=ticket.ticket_number,
            pr_url=inputs.pr_url,
            started_at=start,
            merged_at=merged_at,
            elapsed_seconds=0.0,
            work_seconds=0.0,
            wait_seconds=dict.fromkeys(WAIT_CAUSES, 0.0),
            longest_wait=None,
            hand_cancels=cancels,
            inferred_hand_tasks=inferred,
            measured=False,
            parks_retained=False,
        )

    tagged: list[_Tag] = []
    park_windows: dict[int, list[tuple[datetime, datetime]]] = defaultdict(list)
    for task in inputs.tasks:
        task_tags, task_windows = _task_wait_tags(task, inputs, merged_at)
        tagged.extend(task_tags)
        park_windows[task.pk].extend(task_windows)
    tagged.extend((created, ended or merged_at, cause) for created, ended, cause in inputs.questions)
    tagged.extend((span.entered_at, span.left_at, WaitCause.REVIEW) for span in inputs.review_spans)
    tagged = [(max(start, lower), min(merged_at, upper), cause) for lower, upper, cause in tagged]
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
    return MergeWaitBreakdown(
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
        inferred_hand_tasks=inferred,
        measured=True,
        parks_retained=start >= cutoff,
    )


def _split_work_and_waits(
    start: datetime,
    merged_at: datetime,
    tagged: Sequence[_Tag],
    work: Sequence[tuple[datetime, datetime]],
) -> tuple[float, dict[WaitCause, float], WaitBlock | None]:
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
            WaitCause.NO_NEXT_TASK,
        )
        waits[cause] += seconds
        if blocks and blocks[-1].cause == cause and blocks[-1].end == lower:
            blocks[-1] = WaitBlock(cause=cause, start=blocks[-1].start, end=upper)
        else:
            blocks.append(WaitBlock(cause=cause, start=lower, end=upper))
    longest = max(blocks, key=attrgetter("seconds")) if blocks else None
    return work_seconds, waits, longest


def _task_wait_tags(
    task: Task, inputs: _MergeInputs, merged_at: datetime
) -> tuple[list[_Tag], list[tuple[datetime, datetime]]]:
    tags: list[_Tag] = []
    attempts = inputs.attempts.get(task.pk, ())
    parks = sorted((attempt for attempt in attempts if attempt.parked), key=attrgetter("started_at"))
    terminal_at = (
        max(
            (attempt.ended_at for attempt in attempts if not attempt.parked and attempt.ended_at is not None),
            default=None,
        )
        if task.status in Task.Status.terminal()
        else None
    )
    sibling_holders = {task_holder_id(other) for other in inputs.tasks if other.pk != task.pk}
    windows: list[tuple[datetime, datetime]] = []
    for index, park in enumerate(parks):
        next_park = parks[index + 1].started_at if index + 1 < len(parks) else merged_at
        readmitted = task.admitted_at if task.admitted_at and task.admitted_at > park.started_at else merged_at
        end = min(
            next_park,
            readmitted,
            terminal_at or merged_at,
            merged_at,
        )
        if end <= park.started_at:
            continue
        cause = next(
            (cause for prefix, cause in _PARK_CAUSES.items() if park.error.startswith(prefix)),
            WaitCause.CHECKOUT_FENCE,
        )
        if cause == WaitCause.CHECKOUT_FENCE and park.holder in sibling_holders:
            cause = WaitCause.SIBLING_TASK
        tags.append((park.started_at, end, cause))
        windows.append((max(park.started_at, park.ended_at) if park.billed and park.ended_at else park.started_at, end))
    if (seat := _seat_wait(task, merged_at)) is not None:
        # admitted_at is re-stamped on every admission, so the seat wait would swallow earlier review holds.
        holds = [(lower, upper) for lower, upper, cause in tags if cause == WaitCause.REVIEW]
        tags.extend((lower, upper, seat[2]) for lower, upper in subtract_intervals(((seat[0], seat[1]),), holds))
    return tags, windows


def _seat_wait(task: Task, merged_at: datetime) -> _Tag | None:
    if task.created_at and task.admitted_at is not None:
        return task.created_at, task.admitted_at, WaitCause.FREE_SEAT
    if task.created_at and task.status not in Task.Status.terminal():
        return task.created_at, merged_at, WaitCause.NOT_ADMITTED
    return None


def _readmission_obscures_work(inputs: _MergeInputs, merged_at: datetime, start: datetime) -> bool:
    for task in inputs.tasks:
        attempts = inputs.attempts.get(task.pk, ())
        if any(attempt.parked and attempt.started_at < start for attempt in attempts):
            return True
        if (
            task.status in Task.Status.terminal()
            and any(attempt.parked and attempt.started_at < merged_at for attempt in attempts)
            and not any(
                not attempt.parked and attempt.ended_at is not None and attempt.ended_at <= merged_at
                for attempt in attempts
            )
        ):
            return True
        real_ends = [
            attempt.ended_at
            for attempt in attempts
            if attempt.ended_at is not None
            and attempt.ended_at <= merged_at
            and (not attempt.parked or attempt.billed)
            and (task.status != Task.Status.FAILED or not attempt.error or attempt.billed)
            and attempt.cancel is None
        ]
        if not real_ends:
            continue
        admitted = task.admitted_at
        if admitted is None or any(ended < admitted for ended in real_ends):
            return True
        if any(
            park.started_at < ended and admitted <= park.started_at
            for park in attempts
            if park.parked and not park.billed
            for ended in real_ends
        ):
            return True
    return False


def _inferred_hand_tasks(tasks: Sequence[Task], first_session_id: int | None) -> int:
    roots = [task for task in tasks if task.session_id == first_session_id and task.parent_task_id is None]
    first_planning_id = min((task.pk for task in roots if normalize_phase(task.phase) == "planning"), default=None)
    return sum(task.pk != first_planning_id for task in roots)
