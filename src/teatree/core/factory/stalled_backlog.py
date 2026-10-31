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

A newest failure that is explicitly parked — stamped ``[repair-halt-parked]`` or CANCELLED — is a recorded
decision, not a stranding, at any age: the retire-or-keep card of
:mod:`teatree.core.factory.parked_ticket_decision` asks the owner about the aged ones. An unstamped
failure still counts, so a lane that is down keeps raising the aggregate signal.

Separated from :mod:`teatree.core.factory.operational_health` the way
``reclaim_is_stalled`` is — the aggregator folds the collected signals into a verdict;
this module owns what "stranded" means and the one signal it raises.
"""

import logging
from datetime import timedelta

from django.db.models import Exists, Max, OuterRef, Subquery
from django.utils import timezone

from teatree.core.factory.health_signal import HealthSignal, SignalCollection
from teatree.core.modelkit.task_failure_taxonomy import HALT_STAMP, FailureKind
from teatree.core.models import Task, Ticket
from teatree.core.models.known_issue import KnownIssue

#: A ticket whose newest task FAILED this long ago, with nothing pending or claimed, has
#: no execution path left — long enough that a retry in flight never trips it.
STALLED_BACKLOG_WINDOW = timedelta(hours=2)
#: One stranded ticket is churn; several at once is the dispatch lane itself being down.
STALLED_BACKLOG_THRESHOLD = 3
logger = logging.getLogger(__name__)


def stranded_ticket_count() -> int:
    """Tickets in WORK_STARTED whose newest task FAILED outside the window, with none in flight."""
    now = timezone.now()
    own_tasks = Task.objects.filter(ticket_id=OuterRef("pk"))
    newest = own_tasks.order_by("-created_at", "-pk")
    return (
        Ticket.objects.filter(state=Ticket.State.WORK_STARTED)
        .annotate(
            newest_task_at=Max("tasks__created_at"),
            # The NEWEST status, not "has a failed task": a later completion retires the failure.
            newest_task_status=Subquery(newest.values("status")[:1]),
            newest_task_failure_kind=Subquery(newest.values("failure_kind")[:1]),
            newest_task_reason=Subquery(newest.values("execution_reason")[:1]),
        )
        # The window is what separates a stranded ticket from one whose retry is in flight.
        .filter(newest_task_at__lt=now - STALLED_BACKLOG_WINDOW)
        .filter(newest_task_status=Task.Status.FAILED)
        .exclude(Exists(own_tasks.filter(status__in=Task.Status.active())))
        .exclude(newest_task_failure_kind=FailureKind.CANCELLED)
        .exclude(newest_task_reason__contains=HALT_STAMP)
        .count()
    )


def stalled_backlog_signals() -> SignalCollection:
    """One CRITICAL when admitted work has been left with no execution path (#4704).

    What "stranded" means, and why neither ``_failed_task_signals`` nor
    ``StaleTicketsScanner`` covers it, is this module's docstring.
    Fail-open, naming itself ``unread`` on a read it could not make.
    """
    try:
        stalled = stranded_ticket_count()
    except Exception:
        logger.exception("stalled-backlog health read failed — skipped")
        return SignalCollection(unread=("stalled_backlog_signals",))
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
