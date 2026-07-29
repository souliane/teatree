"""Retention pruning for the high-churn control-DB tables (#3693, #3871).

The control DB has no cleanup path for the tables that grow per dispatch. This is the
sanctioned prune. Every lane deletes rows whose owning ticket/task is TERMINAL, never a
live or in-flight row, and each resolves its row set through a single named predicate so
"safe to delete" is defined in exactly one place per lane.

``TaskAttempt`` and ``IncomingEvent`` resolve through their managers' ``prunable``
querysets, age-based (#3693).

``TaskAttempt`` gets a SECOND lane, because the terminal-owned one can never reach a
limit-park: a park RETURNS its task to the queue PENDING, so the park row's owning task
is non-terminal and the guard excludes it forever. A park-bloated table therefore
reported "would prune 0 row(s)" — the remedy prescribed for the condition being flagged
could not touch it. The park lane keys on the canonical ``limit_parked:`` marker under
its own, shorter window, and never deletes a row carrying billed telemetry. See
:meth:`TaskAttemptQuerySet.prunable_parks` for the three questions it asks.

``TicketTransition`` resolves through :meth:`TicketTransitionQuerySet.prunable`, which
is keyed on the ticket CLOSING rather than on a row aging, and decides per ROW rather
than per table: a ``from_state == to_state`` row records no edge, so it is not history
and a reopened ticket does not need it. Every real state edge survives for as long as
the ticket does — ~410 rows on the measured box.

``DBTaskResult`` goes through :mod:`teatree.core.retention.task_results`, which delegates
the delete to ``django_tasks_db``'s OWN shipped ``prune_db_task_results`` command rather
than teatree writing a second prune over a dependency's table.

Destructive, so the default is planning, not deleting: :func:`plan_retention` reports
what WOULD be pruned (read-only); :func:`apply_retention` is the only path that deletes.
"""

import dataclasses
import datetime as dt

from django.db import models, transaction
from django.utils import timezone

from teatree.config import get_effective_settings
from teatree.config.settings import UserSettings
from teatree.core.models import IncomingEvent, TaskAttempt
from teatree.core.models.transition import TicketTransition
from teatree.core.models.usage_window_state import LIMIT_PARKED_PREFIX
from teatree.core.retention.task_results import (
    prunable_task_results,
    prune_finished_task_results,
    task_results_are_stored_in_the_db,
)

#: Rows deleted per committed statement on the batched lanes. A single ``DELETE`` over
#: the whole set holds the SQLite write lock for its entire duration — on the measured
#: residues (330k park rows, 3.2M transition rows) that is long enough to collide with a
#: converging deploy's migration step. Each batch commits on its own, so the lock is
#: released between them and an interrupted run simply leaves fewer rows: both lanes are
#: idempotent and order-independent, and nothing references either row.
DELETE_BATCH_SIZE = 5_000

#: The labels each lane reports under, so an operator can see which rule acted.
PARK_TABLE = "TaskAttempt (park)"
TRANSITION_TABLE = "TicketTransition"
TASK_RESULT_TABLE = "DBTaskResult"


@dataclasses.dataclass(frozen=True, slots=True)
class TableRetention:
    """One table's retention outcome — planned (would-delete) or applied (deleted)."""

    table: str
    retention_days: int
    #: Rows the policy acts on: the would-delete count in a plan, the deleted count after apply.
    rows: int
    #: Subset of ``rows`` that is limit-park junk (``TaskAttempt`` only; ``0`` elsewhere).
    junk: int = 0
    #: True when this lane is off and nothing was touched — see ``reason``.
    disabled: bool = False
    #: Why a disabled lane is off, when it is not simply a ``0`` window.
    reason: str = ""
    #: False for a lane whose rule is redundancy rather than age, so a report does not
    #: claim its rows were older than a window they were never measured against.
    aged: bool = True
    #: Committed delete statements this lane took (batched lanes only; ``0`` elsewhere).
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


def _disabled(table: str, days: int, reason: str = "") -> TableRetention:
    return TableRetention(table, days, 0, disabled=True, reason=reason)


#: Reported when the default task backend is not the one that writes ``DBTaskResult``
#: rows, so there is no result table for this lane to act on.
_NO_RESULT_TABLE = "the default task backend does not store results in the DB"
#: The transition lane has no window — it is keyed on ticket closure — so its kill
#: switch reports its own name rather than a meaningless ``retention_days=0``.
_TRANSITION_OFF = "ticket_transition_prune_disabled"


def _junk_count(task_attempt_qs: models.QuerySet) -> int:
    return task_attempt_qs.filter(error__startswith=LIMIT_PARKED_PREFIX).count()


def _delete_in_batches(resolve: "models.QuerySet", *, batch_size: int) -> tuple[int, int]:
    """Delete *resolve*'s rows in separately-committed batches; return ``(deleted, batches)``.

    Deliberately OUTSIDE the age lanes' single transaction. Their all-or-nothing
    semantics fit a small set; these are measured in hundreds of thousands, and holding
    one write lock across them is a real operational hazard on SQLite. Each batch
    re-resolves the predicate and deletes by the pks just selected, so the slice and the
    delete describe the same rows even if a writer adds one between them.
    """
    deleted = 0
    batches = 0
    while True:
        with transaction.atomic():
            batch = list(resolve.values_list("pk", flat=True)[:batch_size])
            if not batch:
                return deleted, batches
            resolve.filter(pk__in=batch).delete()
            batches += 1
            deleted += len(batch)
        if len(batch) < batch_size:
            return deleted, batches


def _plan_parks(moment: dt.datetime, cfg: UserSettings) -> TableRetention:
    days = int(cfg.park_attempt_retention_days)
    if days <= 0:
        return _disabled(PARK_TABLE, days)
    rows = TaskAttempt.objects.prunable_parks(_cutoff(moment, days)).count()
    return TableRetention(PARK_TABLE, days, rows, junk=rows)


def _apply_parks(moment: dt.datetime, cfg: UserSettings, *, batch_size: int) -> TableRetention:
    days = int(cfg.park_attempt_retention_days)
    if days <= 0:
        return _disabled(PARK_TABLE, days)
    resolve = TaskAttempt.objects.prunable_parks(_cutoff(moment, days))
    rows, batches = _delete_in_batches(resolve, batch_size=batch_size)
    return TableRetention(PARK_TABLE, days, rows, junk=rows, batches=batches)


def _plan_transition_lane(cfg: UserSettings) -> TableRetention:
    if cfg.ticket_transition_prune_disabled:
        return _disabled(TRANSITION_TABLE, 0, _TRANSITION_OFF)
    return TableRetention(TRANSITION_TABLE, 0, TicketTransition.objects.prunable().count(), aged=False)


def _apply_transition_lane(cfg: UserSettings, *, batch_size: int) -> TableRetention:
    if cfg.ticket_transition_prune_disabled:
        return _disabled(TRANSITION_TABLE, 0, _TRANSITION_OFF)
    rows, batches = _delete_in_batches(TicketTransition.objects.prunable(), batch_size=batch_size)
    return TableRetention(TRANSITION_TABLE, 0, rows, aged=False, batches=batches)


def _task_result_lane_days(cfg: UserSettings) -> int | None:
    """The lane's window, or ``None`` when it must not run."""
    days = int(cfg.task_result_retention_days)
    return days if days > 0 and task_results_are_stored_in_the_db() else None


def _task_result_disabled(cfg: UserSettings) -> TableRetention:
    days = int(cfg.task_result_retention_days)
    reason = "" if days <= 0 else _NO_RESULT_TABLE
    return _disabled(TASK_RESULT_TABLE, days, reason)


def _plan_task_result_lane(moment: dt.datetime, cfg: UserSettings) -> TableRetention:
    days = _task_result_lane_days(cfg)
    if days is None:
        return _task_result_disabled(cfg)
    return TableRetention(TASK_RESULT_TABLE, days, prunable_task_results(_cutoff(moment, days)).count())


def _apply_task_result_lane(cfg: UserSettings) -> TableRetention:
    days = _task_result_lane_days(cfg)
    if days is None:
        return _task_result_disabled(cfg)
    return TableRetention(TASK_RESULT_TABLE, days, prune_finished_task_results(days=days))


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

    tables.extend((_plan_transition_lane(cfg), _plan_task_result_lane(moment, cfg)))

    return RetentionPlan(moment, tuple(tables))


def apply_retention(
    now: dt.datetime | None = None,
    *,
    settings: UserSettings | None = None,
    batch_size: int = DELETE_BATCH_SIZE,
) -> RetentionPlan:
    """Delete the prunable rows per lane, returning the applied plan.

    The two age lanes over ``TaskAttempt``/``IncomingEvent`` run inside one transaction
    (all-or-nothing, matching #3693). The park and transition lanes run after it in
    separately-committed batches (:func:`_delete_in_batches`), and the ``DBTaskResult``
    lane last, through the library's own command.
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
    tables.extend((_apply_transition_lane(cfg, batch_size=batch_size), _apply_task_result_lane(cfg)))
    return RetentionPlan(moment, tuple(tables), applied=True)
