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

from django.utils import timezone

from teatree.core.factory.health_signal import HealthSignal, SignalCollection
from teatree.core.factory.parked_tickets import silent_freeze_tasks
from teatree.core.models import Ticket
from teatree.core.models.known_issue import KnownIssue
from teatree.utils.throttled_log import warn_throttled

#: A ticket whose newest task FAILED this long ago, with nothing pending or claimed, has
#: no execution path left — long enough that a retry in flight never trips it.
STALLED_BACKLOG_WINDOW = timedelta(hours=2)
#: One stranded ticket is churn; several at once is the dispatch lane itself being down.
STALLED_BACKLOG_THRESHOLD = 3
logger = logging.getLogger(__name__)


def stranded_ticket_count() -> int:
    """Tickets in WORK_STARTED whose newest task FAILED outside the window, with none in flight."""
    cutoff = timezone.now() - STALLED_BACKLOG_WINDOW
    return sum(
        task.ticket.state == Ticket.State.WORK_STARTED and task.created_at is not None and task.created_at < cutoff
        for task in silent_freeze_tasks()
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
                    f"{stalled} tickets stranded in started — newest task failed over {hours}h ago "
                    "and nothing is pending or claimed; the dispatch lane is not running"
                ),
            ),
        )
    )
