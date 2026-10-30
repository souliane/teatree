"""How much admitted work has been left with no execution path (souliane/teatree#4704).

A dispatch that dies BEFORE the claim leaves no trace on any surface an operator reads:
the ticket sits in WORK_STARTED looking freshly admitted, intake reports the issue as already
picked up, and the loop keeps ticking and stamping its last run. Fifteen tickets stalled
that way for a day behind a Claude CLI pin the API had stopped accepting, and the only
signal anywhere was a line in the worker log.

Neither existing surface covers it. ``_failed_task_signals``' window closes after six
hours, and a lane that is down stays down. ``StaleTicketsScanner`` measures generic
inactivity over ``stale_threshold_days`` (3) and routes to the statusline's
``action_needed`` zone, so a dead lane is a quiet row three days later rather than a red
chip in two hours.

The tell here is durable and structural: the ticket was queued, its newest task FAILED,
and nothing is pending, claimed, or retrying. Requiring FAILED rather than merely terminal
keeps a ticket a human is hand-working (newest task COMPLETED) out of the count.

Separated from :mod:`teatree.core.factory.operational_health` the way
``reclaim_is_stalled`` is — the aggregator owns folding signals into a verdict, the
predicate owns what "stranded" means.
"""

import logging
from datetime import timedelta

from django.db.models import Exists, Max, OuterRef, Subquery
from django.utils import timezone

from teatree.core.factory.health_signal import HealthSignal, SignalCollection
from teatree.core.modelkit.task_failure_taxonomy import HALT_STAMP, FailureKind
from teatree.core.models import Task, Ticket
from teatree.core.models.known_issue import KnownIssue
from teatree.utils.throttled_log import warn_throttled

STALLED_BACKLOG_WINDOW = timedelta(hours=2)
#: One stranded ticket is churn; several at once is the dispatch lane itself being down.
STALLED_BACKLOG_THRESHOLD = 3
logger = logging.getLogger(__name__)


def stranded_ticket_count() -> int:
    """Tickets in WORK_STARTED whose newest task FAILED outside the window, with none in flight."""
    own_tasks = Task.objects.filter(ticket_id=OuterRef("pk"))
    newest = own_tasks.order_by("-created_at", "-pk")
    return (
        Ticket.objects.filter(state=Ticket.State.WORK_STARTED)
        .annotate(
            newest_task_at=Max("tasks__created_at"),
            newest_task_status=Subquery(newest.values("status")[:1]),
            newest_task_reason=Subquery(newest.values("execution_reason")[:1]),
            newest_task_failure_kind=Subquery(newest.values("failure_kind")[:1]),
        )
        .filter(newest_task_at__lt=timezone.now() - STALLED_BACKLOG_WINDOW)
        .filter(newest_task_status=Task.Status.FAILED)
        .exclude(Exists(own_tasks.filter(status__in=Task.Status.active())))
        .exclude(newest_task_reason__contains=HALT_STAMP)
        .exclude(newest_task_failure_kind=FailureKind.CANCELLED)
        .count()
    )


def _stalled_backlog_signals() -> SignalCollection:
    try:
        stalled = stranded_ticket_count()
    except Exception:  # noqa: BLE001 — a failed health read must not auto-resolve an existing issue
        warn_throttled(logger, "health-stalled-backlog", "stalled-backlog health read failed — skipped", exc_info=True)
        return SignalCollection(unread=("_stalled_backlog_signals",))
    if stalled < STALLED_BACKLOG_THRESHOLD:
        return SignalCollection()
    hours = int(STALLED_BACKLOG_WINDOW.total_seconds() // 3600)
    return SignalCollection(
        (
            HealthSignal(
                fingerprint="stalled-backlog",
                severity=KnownIssue.Severity.CRITICAL,
                kind="stalled_backlog",
                summary=(
                    f"{stalled} tickets stranded in started — newest task created over {hours}h ago and failed, "
                    "with nothing pending or claimed; dispatch needs investigation"
                ),
            ),
        )
    )
