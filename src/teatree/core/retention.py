"""Age-based retention pruning for the high-churn control-DB tables (#3693).

The control DB has no cleanup path for the tables that grow per dispatch —
``TaskAttempt`` (one row per attempt/park; the ~340k park-spin residue) and
``IncomingEvent`` (one per inbound webhook). This is the sanctioned prune: it
deletes rows OLDER than a per-table configurable window whose owning ticket/task
is TERMINAL, never a live or in-flight row.

Destructive, so the default is planning, not deleting: :func:`plan_retention`
reports what WOULD be pruned (read-only); :func:`apply_retention` is the only path
that deletes. The safety predicates (:meth:`TaskAttemptQuerySet.prunable` /
:meth:`TaskAttemptQuerySet.prunable_parks` /
:meth:`IncomingEventQuerySet.prunable`) are the SINGLE audit point for "safe to
delete" — both the plan and the apply resolve their row set through them, so each
guard is defined in exactly one place.

**Two lanes over ``TaskAttempt``, not one.** The terminal-owned lane protects real
attempts and is deliberately conservative. It also, by construction, can never
reach a limit-park: a park RETURNS its task to the queue PENDING, so the park row's
owning task is non-terminal and the guard excludes it forever. A park-bloated table
therefore reported "would prune 0 row(s)" — the remedy prescribed for the condition
being flagged could not touch it. The park lane closes that: it keys on the
canonical ``limit_parked:`` marker under its own, shorter window, and never deletes
a row carrying billed telemetry. See :meth:`TaskAttemptQuerySet.prunable_parks` for
the three questions it asks.
"""

import dataclasses
import datetime as dt

from django.db import models, transaction
from django.utils import timezone

from teatree.config import get_effective_settings
from teatree.config.settings import UserSettings
from teatree.core.models import IncomingEvent, TaskAttempt
from teatree.core.models.usage_window_state import LIMIT_PARKED_PREFIX

#: Rows deleted per committed statement on the park lane. A single ``DELETE`` over
#: the whole set holds the SQLite write lock for its entire duration — on the
#: measured 330k-row residue that is long enough to collide with a converging
#: deploy's migration step. Each batch commits on its own, so the lock is released
#: between them and an interrupted run simply leaves fewer park rows: the lane is
#: idempotent and order-independent, and no row references a ``TaskAttempt``.
PARK_DELETE_BATCH_SIZE = 5_000

#: The label the park lane reports under, distinct from the terminal-owned lane's
#: so an operator can see which rule acted.
PARK_TABLE = "TaskAttempt (park)"


@dataclasses.dataclass(frozen=True, slots=True)
class TableRetention:
    """One table's retention outcome — planned (would-delete) or applied (deleted)."""

    table: str
    retention_days: int
    #: Rows the policy acts on: the would-delete count in a plan, the deleted count after apply.
    rows: int
    #: Subset of ``rows`` that is limit-park junk (``TaskAttempt`` only; ``0`` elsewhere).
    junk: int = 0
    #: True when this table's window is ``0`` — retention is disabled and nothing is touched.
    disabled: bool = False
    #: Committed delete statements this lane took (park lane only; ``0`` elsewhere).
    batches: int = 0


@dataclasses.dataclass(frozen=True, slots=True)
class RetentionPlan:
    now: dt.datetime
    tables: tuple[TableRetention, ...]
    applied: bool = False

    @property
    def total_rows(self) -> int:
        return sum(table.rows for table in self.tables)


def _now(now: dt.datetime | None) -> dt.datetime:
    return now if now is not None else timezone.now()


def _cutoff(now: dt.datetime, days: int) -> dt.datetime:
    return now - dt.timedelta(days=days)


def _junk_count(task_attempt_qs: models.QuerySet) -> int:
    return task_attempt_qs.filter(error__startswith=LIMIT_PARKED_PREFIX).count()


def _disabled(table: str, days: int) -> TableRetention:
    return TableRetention(table, days, 0, disabled=True)


def _plan_parks(moment: dt.datetime, cfg: UserSettings) -> TableRetention:
    days = int(cfg.park_attempt_retention_days)
    if days <= 0:
        return _disabled(PARK_TABLE, days)
    rows = TaskAttempt.objects.prunable_parks(_cutoff(moment, days)).count()
    return TableRetention(PARK_TABLE, days, rows, junk=rows)


def _apply_parks(moment: dt.datetime, cfg: UserSettings, *, batch_size: int) -> TableRetention:
    """Delete the prunable park rows in separately-committed batches.

    Deliberately OUTSIDE the terminal-owned lane's single transaction. That lane's
    all-or-nothing semantics are right for rows whose deletion is entangled with a
    ticket's history; a park row is an independently disposable scheduling event, so
    partial progress is a correct outcome — and holding one write lock across the
    whole set is a real operational hazard, since a concurrent deploy's migration
    step blocks on it.
    """
    days = int(cfg.park_attempt_retention_days)
    if days <= 0:
        return _disabled(PARK_TABLE, days)
    cutoff = _cutoff(moment, days)
    deleted = 0
    batches = 0
    while True:
        with transaction.atomic():
            # Delete by the pks just selected rather than by re-running the predicate:
            # the slice and the delete then describe the same rows even if a poller
            # writes a new park between them.
            batch = list(TaskAttempt.objects.prunable_parks(cutoff).values_list("pk", flat=True)[:batch_size])
            if not batch:
                return TableRetention(PARK_TABLE, days, deleted, junk=deleted, batches=batches)
            TaskAttempt.objects.filter(pk__in=batch).delete()
            batches += 1
            deleted += len(batch)
        if len(batch) < batch_size:
            return TableRetention(PARK_TABLE, days, deleted, junk=deleted, batches=batches)


def plan_retention(now: dt.datetime | None = None, *, settings: UserSettings | None = None) -> RetentionPlan:
    """Report what retention WOULD prune, per lane. Read-only — deletes nothing."""
    moment = _now(now)
    cfg = settings or get_effective_settings()

    tables: list[TableRetention] = []

    ta_days = int(cfg.task_attempt_retention_days)
    if ta_days > 0:
        qs = TaskAttempt.objects.prunable(_cutoff(moment, ta_days))
        tables.append(TableRetention("TaskAttempt", ta_days, qs.count(), junk=_junk_count(qs)))
    else:
        tables.append(_disabled("TaskAttempt", ta_days))

    tables.append(_plan_parks(moment, cfg))

    ie_days = int(cfg.incoming_event_retention_days)
    if ie_days > 0:
        qs = IncomingEvent.objects.prunable(_cutoff(moment, ie_days))
        tables.append(TableRetention("IncomingEvent", ie_days, qs.count()))
    else:
        tables.append(_disabled("IncomingEvent", ie_days))

    return RetentionPlan(moment, tuple(tables))


def apply_retention(
    now: dt.datetime | None = None,
    *,
    settings: UserSettings | None = None,
    batch_size: int = PARK_DELETE_BATCH_SIZE,
) -> RetentionPlan:
    """Delete the prunable rows per lane, returning the applied plan.

    The two terminal-owned lanes run inside one transaction (all-or-nothing); the
    park lane runs after it in separately-committed batches — see :func:`_apply_parks`.
    """
    moment = _now(now)
    cfg = settings or get_effective_settings()

    tables: list[TableRetention] = []
    with transaction.atomic():
        ta_days = int(cfg.task_attempt_retention_days)
        if ta_days > 0:
            qs = TaskAttempt.objects.prunable(_cutoff(moment, ta_days))
            junk = _junk_count(qs)
            # Count inside the transaction, before the delete: nothing else mutates
            # the row set here, so the count is exactly what delete() removes — and it
            # avoids depending on the model-label key delete() returns.
            rows = qs.count()
            qs.delete()
            tables.append(TableRetention("TaskAttempt", ta_days, rows, junk=junk))
        else:
            tables.append(_disabled("TaskAttempt", ta_days))

        ie_days = int(cfg.incoming_event_retention_days)
        if ie_days > 0:
            qs = IncomingEvent.objects.prunable(_cutoff(moment, ie_days))
            rows = qs.count()
            qs.delete()
            tables.append(TableRetention("IncomingEvent", ie_days, rows))
        else:
            tables.append(_disabled("IncomingEvent", ie_days))

    tables.insert(1, _apply_parks(moment, cfg, batch_size=batch_size))
    return RetentionPlan(moment, tuple(tables), applied=True)
