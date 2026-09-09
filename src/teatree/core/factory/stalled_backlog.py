"""How much admitted work has been left with no execution path (souliane/teatree#4704).

A dispatch that dies BEFORE the claim leaves no trace on any surface an operator reads:
the ticket sits in STARTED looking freshly admitted, intake reports the issue as already
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

from datetime import timedelta
from typing import TYPE_CHECKING, cast

from django.utils import timezone

if TYPE_CHECKING:
    from teatree.core.models.task import Task
    from teatree.core.models.ticket import Ticket

#: A ticket whose newest task FAILED this long ago, with nothing pending or claimed, has
#: no execution path left — long enough that a retry in flight never trips it.
STALLED_BACKLOG_WINDOW = timedelta(hours=2)
#: One stranded ticket is churn; several at once is the dispatch lane itself being down.
STALLED_BACKLOG_THRESHOLD = 3


def stranded_ticket_count() -> int:
    """Tickets in STARTED whose newest task FAILED outside the window, with none in flight."""
    from django.apps import apps  # noqa: PLC0415 — deferred so the app registry is only touched at read time
    from django.db.models import Exists, Max, OuterRef  # noqa: PLC0415 — deferred with the app registry above

    ticket_model = cast("type[Ticket]", apps.get_model("core", "Ticket"))
    task_model = cast("type[Task]", apps.get_model("core", "Task"))
    in_flight = task_model.objects.filter(ticket=OuterRef("pk"), status__in=task_model.Status.active())
    return (
        ticket_model.objects.filter(state=ticket_model.State.STARTED)
        .annotate(newest_task_at=Max("tasks__created_at"))
        # The window is what separates a stranded ticket from one whose retry is in flight.
        .filter(newest_task_at__lt=timezone.now() - STALLED_BACKLOG_WINDOW)
        .filter(tasks__status=task_model.Status.FAILED)
        .exclude(Exists(in_flight))
        .distinct()
        .count()
    )
