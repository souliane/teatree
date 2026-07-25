"""Age-based retention pruning for the high-churn control-DB tables (#3693).

The control DB has no cleanup path for the tables that grow per dispatch —
``TaskAttempt`` (one row per attempt/park; the ~340k park-spin residue) and
``IncomingEvent`` (one per inbound webhook). This is the sanctioned prune: it
deletes rows OLDER than a per-table configurable window whose owning ticket/task
is TERMINAL, never a live or in-flight row.

Destructive, so the default is planning, not deleting: :func:`plan_retention`
reports what WOULD be pruned (read-only); :func:`apply_retention` is the only path
that deletes, and it does so inside one transaction. The two safety predicates
(:meth:`TaskAttemptQuerySet.prunable` / :meth:`IncomingEventQuerySet.prunable`) are
the SINGLE audit point for "safe to delete" — both the plan and the apply resolve the
row set through them, so the guard is defined in exactly one place.
"""

import dataclasses
import datetime as dt

from django.db import models, transaction
from django.utils import timezone

from teatree.config import get_effective_settings
from teatree.config.settings import UserSettings
from teatree.core.models import IncomingEvent, TaskAttempt
from teatree.core.models.usage_window_state import LIMIT_PARKED_PREFIX


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


def plan_retention(now: dt.datetime | None = None, *, settings: UserSettings | None = None) -> RetentionPlan:
    """Report what retention WOULD prune, per table. Read-only — deletes nothing."""
    moment = _now(now)
    cfg = settings or get_effective_settings()

    tables: list[TableRetention] = []

    ta_days = int(cfg.task_attempt_retention_days)
    if ta_days > 0:
        qs = TaskAttempt.objects.prunable(_cutoff(moment, ta_days))
        tables.append(TableRetention("TaskAttempt", ta_days, qs.count(), junk=_junk_count(qs)))
    else:
        tables.append(TableRetention("TaskAttempt", ta_days, 0, disabled=True))

    ie_days = int(cfg.incoming_event_retention_days)
    if ie_days > 0:
        qs = IncomingEvent.objects.prunable(_cutoff(moment, ie_days))
        tables.append(TableRetention("IncomingEvent", ie_days, qs.count()))
    else:
        tables.append(TableRetention("IncomingEvent", ie_days, 0, disabled=True))

    return RetentionPlan(moment, tuple(tables))


def apply_retention(now: dt.datetime | None = None, *, settings: UserSettings | None = None) -> RetentionPlan:
    """Delete the prunable rows per table, inside one transaction. Returns the applied plan."""
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
            tables.append(TableRetention("TaskAttempt", ta_days, 0, disabled=True))

        ie_days = int(cfg.incoming_event_retention_days)
        if ie_days > 0:
            qs = IncomingEvent.objects.prunable(_cutoff(moment, ie_days))
            rows = qs.count()
            qs.delete()
            tables.append(TableRetention("IncomingEvent", ie_days, rows))
        else:
            tables.append(TableRetention("IncomingEvent", ie_days, 0, disabled=True))

    return RetentionPlan(moment, tuple(tables), applied=True)
