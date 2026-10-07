"""Queued work nobody has claimed or run for a whole window — the one stall predicate.

``t3 doctor check`` passes every PENDING row and the ``dispatch_gap`` detector passes only
the claimable ones, so the two surfaces share one definition of "no progress": a current
claim, or a real attempt started inside the window, is progress; a usage-limit park is not.
"""

import os
from dataclasses import dataclass
from datetime import datetime, timedelta

from django.db.models import QuerySet

from teatree.core.models import Task, TaskAttempt
from teatree.core.models.usage_window_state import LIMIT_PARKED_PREFIX

DEFAULT_STALL_MINUTES = 30
MAX_STALL_MINUTES = 24 * 60


def stall_minutes() -> int:
    """Bound a configurable floor so an invalid override cannot disable the alarm."""
    try:
        minutes = int(os.environ.get("TEATREE_QUEUE_STALL_MINUTES", str(DEFAULT_STALL_MINUTES)))
    except ValueError:
        return DEFAULT_STALL_MINUTES
    return minutes if 1 <= minutes <= MAX_STALL_MINUTES else DEFAULT_STALL_MINUTES


@dataclass(frozen=True, slots=True)
class QueueStall:
    pending: int
    oldest_pk: int
    oldest_created_at: datetime


def read_queue_stall(pending: QuerySet[Task], *, now: datetime, minutes: int) -> QueueStall | None:
    """The stall over *pending*, or ``None`` when its oldest row is young or the queue moved."""
    cutoff = now - timedelta(minutes=minutes)
    oldest = pending.order_by("created_at", "pk").values_list("pk", "created_at").first()
    if oldest is None or oldest[1] >= cutoff:
        return None
    if Task.objects.filter(status=Task.Status.CLAIMED).exists():
        return None
    if TaskAttempt.objects.filter(started_at__gte=cutoff).exclude(error__startswith=LIMIT_PARKED_PREFIX).exists():
        return None
    return QueueStall(pending=pending.count(), oldest_pk=oldest[0], oldest_created_at=oldest[1])
