"""Retention for the high-churn control-DB tables (#3693, #3871).

One ordered lane table drives both the dry run and the delete, so the plan and the
apply cannot disagree about a lane's row set. In run order: limit-park ``TaskAttempt``
rows older than a week (a park returns its task to PENDING, so no ticket-keyed lane can
reach one); the FAILED, then the COMPLETED ``Task`` rows of quiescent tickets
(:mod:`teatree.core.retention.ticket_history`) with their attempts by CASCADE, so
failures go first; ``BotPing`` payloads, blanked rather than deleted because dedup reads
the key and status; settled ``IncomingEvent`` rows; ``TicketTransition`` rows that record
no edge on a finished ticket; ``DBTaskResult`` through ``django_tasks_db``'s own prune.

Every lane deletes through the ORM, in short committed batches of pks re-filtered through
its predicate, so a batch holds the write lock briefly and an interrupted pass only leaves
fewer rows. The scheduled pass (``teatree.loops.timer_reconciler.prune_task_results``)
runs hourly under a shared batch budget spent in lane order; the operator's
``t3 <overlay> retention prune --apply`` drains with no budget. :func:`plan_retention`
reads only; :func:`apply_retention` is the path that deletes.
"""

import dataclasses
import datetime as dt
import functools
import time
from collections.abc import Callable

from django.db import models, transaction
from django.utils import timezone

from teatree.config import get_effective_settings
from teatree.config.settings import UserSettings
from teatree.core.factory.factory_signals import FACTORY_LOOKBACK_DAYS
from teatree.core.models import BotPing, IncomingEvent, Task, TaskAttempt
from teatree.core.models.transition import TicketTransition
from teatree.core.retention.task_results import (
    prunable_task_results,
    prune_finished_task_results,
    task_results_are_stored_in_the_db,
)
from teatree.core.retention.ticket_history import prunable_tasks

#: Rows per committed batch: small enough that a competing writer waits well under SQLite's busy timeout.
BATCH_SIZE = 200
#: A task batch stops before its cascade passes this many attempts; one task alone may exceed it.
TASK_BATCH_MAX_ATTEMPTS = 1_000
#: Non-empty batches one scheduled pass may commit across all budgeted lanes.
SCHEDULED_MAX_BATCHES = 10

#: Post-mortem material only: settled inbound events and sent notification payloads.
POST_MORTEM_RETENTION_DAYS = 30
#: Whether the fleet is parked NOW is what a park tells; a week covers after-the-fact forensics.
PARK_ATTEMPT_RETENTION_DAYS = 7

PARK_TABLE = "TaskAttempt (park)"
FAILED_TASK_TABLE = "Task (failed)"
COMPLETED_TASK_TABLE = "Task (completed)"
PING_PAYLOAD_TABLE = "BotPing (payload)"
INCOMING_EVENT_TABLE = "IncomingEvent"
TRANSITION_TABLE = "TicketTransition"
TASK_RESULT_TABLE = "DBTaskResult"

_NO_RESULT_TABLE = "the default task backend does not store results in the DB"


@dataclasses.dataclass(frozen=True, slots=True)
class TableRetention:
    """One lane's outcome — planned (would act on) or applied (acted on)."""

    table: str
    retention_days: int
    rows: int
    #: Attempts deleted with this lane's tasks, leaving out the park rows lane 1 owns.
    cascaded: int = 0
    disabled: bool = False
    reason: str = ""
    #: False for a lane whose rule is redundancy rather than age.
    aged: bool = True
    batches: int = 0
    #: The longest batch's write-lock time.
    max_batch_ms: int = 0


@dataclasses.dataclass(frozen=True, slots=True)
class RetentionPlan:
    now: dt.datetime
    tables: tuple[TableRetention, ...]
    applied: bool = False
    #: The batch budget ran out while a lane still had rows; the next pass continues.
    budget_exhausted: bool = False

    @property
    def total_rows(self) -> int:
        return sum(table.rows for table in self.tables)

    def counts(self) -> dict[str, int]:
        return {
            **{table.table: table.rows for table in self.tables},
            "cascaded": sum(table.cascaded for table in self.tables),
            "max_batch_ms": max((table.max_batch_ms for table in self.tables), default=0),
            "budget_exhausted": int(self.budget_exhausted),
        }


@dataclasses.dataclass(frozen=True, slots=True)
class Lane:
    table: str
    days: int
    resolve: Callable[[dt.datetime], models.QuerySet]
    compact: bool = False
    aged: bool = True

    @property
    def disabled(self) -> bool:
        return self.aged and self.days <= 0

    def rows(self, moment: dt.datetime) -> models.QuerySet:
        return self.resolve(moment - dt.timedelta(days=self.days))


def _task_history_days(cfg: UserSettings) -> int:
    days = int(cfg.task_attempt_retention_days)
    return max(days, FACTORY_LOOKBACK_DAYS) if days > 0 else 0


def _closed_ticket_non_edges(_cutoff: dt.datetime) -> models.QuerySet:
    return TicketTransition.objects.prunable()


def _lanes(cfg: UserSettings) -> tuple[Lane, ...]:
    history_days = _task_history_days(cfg)
    return (
        Lane(PARK_TABLE, PARK_ATTEMPT_RETENTION_DAYS, TaskAttempt.objects.prunable_parks),
        Lane(FAILED_TASK_TABLE, history_days, functools.partial(prunable_tasks, status=Task.Status.FAILED)),
        Lane(COMPLETED_TASK_TABLE, history_days, functools.partial(prunable_tasks, status=Task.Status.COMPLETED)),
        Lane(PING_PAYLOAD_TABLE, POST_MORTEM_RETENTION_DAYS, BotPing.compactable, compact=True),
        Lane(INCOMING_EVENT_TABLE, POST_MORTEM_RETENTION_DAYS, IncomingEvent.objects.prunable),
        Lane(TRANSITION_TABLE, 0, _closed_ticket_non_edges, aged=False),
    )


def _disabled(table: str, days: int, reason: str = "") -> TableRetention:
    return TableRetention(table, days, 0, disabled=True, reason=reason)


def _plan_lane(lane: Lane, moment: dt.datetime) -> TableRetention:
    if lane.disabled:
        return _disabled(lane.table, lane.days)
    rows = lane.rows(moment)
    cascaded = 0
    if rows.model is Task:
        parks = TaskAttempt.objects.prunable_parks(moment - dt.timedelta(days=PARK_ATTEMPT_RETENTION_DAYS))
        cascaded = TaskAttempt.objects.filter(task__in=rows).exclude(pk__in=parks).count()
    return TableRetention(lane.table, lane.days, rows.count(), cascaded=cascaded, aged=lane.aged)


def _next_batch(rows: models.QuerySet) -> list[int]:
    if rows.model is not Task:
        return list(rows.order_by("pk").values_list("pk", flat=True)[:BATCH_SIZE])
    batch: list[int] = []
    attempts = 0
    sized = rows.annotate(attempt_count=models.Count("attempts")).order_by("pk")
    for pk, attempt_count in sized.values_list("pk", "attempt_count")[:BATCH_SIZE]:
        if batch and attempts + attempt_count > TASK_BATCH_MAX_ATTEMPTS:
            break
        batch.append(pk)
        attempts += attempt_count
    return batch


@dataclasses.dataclass(slots=True)
class _ApplyPass:
    moment: dt.datetime
    budget: int | None
    exhausted: bool = False

    def drain(self, lane: Lane) -> TableRetention:
        if lane.disabled:
            return _disabled(lane.table, lane.days)
        rows = cascaded = batches = longest_ms = 0
        while True:
            pending = lane.rows(self.moment)
            if self.budget == 0:
                self.exhausted = self.exhausted or pending.exists()
                break
            batch = _next_batch(pending)
            if not batch:
                break
            started = time.monotonic()
            with transaction.atomic():
                acted, attempts = self._act(lane, pending.filter(pk__in=batch))
            longest_ms = max(longest_ms, round((time.monotonic() - started) * 1000))
            rows += acted
            cascaded += attempts
            batches += 1
            if self.budget is not None:
                self.budget -= 1
        return TableRetention(
            lane.table,
            lane.days,
            rows,
            cascaded=cascaded,
            aged=lane.aged,
            batches=batches,
            max_batch_ms=longest_ms,
        )

    @staticmethod
    def _act(lane: Lane, batch: models.QuerySet) -> tuple[int, int]:
        if lane.compact:
            return batch.update(text="", error_message=""), 0
        attempts = TaskAttempt.objects.filter(task__in=batch).count() if batch.model is Task else 0
        deleted, _ = batch.delete()
        return deleted - attempts, attempts


def _task_result_lane_days(cfg: UserSettings) -> int | None:
    days = int(cfg.task_result_retention_days)
    return days if days > 0 and task_results_are_stored_in_the_db() else None


def _task_result_disabled(cfg: UserSettings) -> TableRetention:
    days = int(cfg.task_result_retention_days)
    return _disabled(TASK_RESULT_TABLE, days, "" if days <= 0 else _NO_RESULT_TABLE)


def _plan_task_result_lane(moment: dt.datetime, cfg: UserSettings) -> TableRetention:
    days = _task_result_lane_days(cfg)
    if days is None:
        return _task_result_disabled(cfg)
    cutoff = moment - dt.timedelta(days=days)
    return TableRetention(TASK_RESULT_TABLE, days, prunable_task_results(cutoff).count())


def _apply_task_result_lane(cfg: UserSettings) -> TableRetention:
    days = _task_result_lane_days(cfg)
    if days is None:
        return _task_result_disabled(cfg)
    return TableRetention(TASK_RESULT_TABLE, days, prune_finished_task_results(days=days))


def plan_retention(now: dt.datetime | None = None, *, settings: UserSettings | None = None) -> RetentionPlan:
    """Report what retention WOULD act on, per lane. Read-only."""
    moment = now or timezone.now()
    cfg = settings or get_effective_settings()
    tables = [_plan_lane(lane, moment) for lane in _lanes(cfg)]
    tables.append(_plan_task_result_lane(moment, cfg))
    return RetentionPlan(moment, tuple(tables))


def apply_retention(
    now: dt.datetime | None = None,
    *,
    settings: UserSettings | None = None,
    max_batches: int | None = None,
) -> RetentionPlan:
    """Act on every lane in order, spending at most *max_batches* batches; ``None`` drains everything.

    The ``DBTaskResult`` lane runs on every pass, outside the budget: it is one library DELETE.
    """
    moment = now or timezone.now()
    cfg = settings or get_effective_settings()
    run = _ApplyPass(moment, max_batches)
    tables = [run.drain(lane) for lane in _lanes(cfg)]
    tables.append(_apply_task_result_lane(cfg))
    return RetentionPlan(moment, tuple(tables), applied=True, budget_exhausted=run.exhausted)
