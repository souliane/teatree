"""``SessionQuerySet`` — Session scoping plus the bounded-liveness query.

A leaf split out of ``managers.py`` (module-health cap). ``Session.ended_at``
marks a session finished, but an agent that crashes never writes it, so "open"
alone would pin its ticket busy forever — and with it the ticket's worktree, its
``max_concurrent_local_stacks`` slot, and ``workspace relocate``. A session is
LIVE only while it is open AND something happened on it within
``session_stale_after_hours``. The bound cannot mask real in-flight work: an
active (PENDING/CLAIMED) ``Task`` keeps a ticket busy with no time bound at all
(``Ticket.has_active_work``), and ``session_stale_after_hours = 0`` restores the
unbounded reading.
"""

from datetime import datetime, timedelta

from django.db import models
from django.db.models import Max
from django.db.models.functions import Coalesce, Greatest
from django.utils import timezone

from teatree.config import get_effective_settings
from teatree.core.managers_overlay import for_overlay as _for_overlay

__all__ = ["SessionQuerySet"]

# The last instant anything happened on a Session: its own creation, the last
# heartbeat of a task it owns, or the start of that task's most recent attempt.
# Each aggregate is COALESCEd against ``started_at`` because SQLite's scalar
# ``MAX()`` returns NULL as soon as one argument is NULL (a session with no
# tasks, or a task with no attempt), which would read as "no activity ever".
_LAST_ACTIVITY = Greatest(
    "started_at",
    Coalesce(Max("tasks__heartbeat_at"), "started_at"),
    Coalesce(Max("tasks__attempts__started_at"), "started_at"),
)


class SessionQuerySet(models.QuerySet):
    def for_overlay(self, overlay: str | None = None) -> models.QuerySet:
        return _for_overlay(self, overlay)

    def for_agent(self, agent_id: str) -> models.QuerySet:
        return self.filter(agent_id=agent_id).order_by("pk")

    def live(self, *, now: datetime | None = None) -> models.QuerySet:
        """Open sessions whose last recorded activity is inside the staleness window."""
        hours = get_effective_settings().session_stale_after_hours
        opened = self.filter(ended_at__isnull=True)
        if hours <= 0:
            return opened
        cutoff = (now or timezone.now()) - timedelta(hours=hours)
        return opened.annotate(last_activity=_LAST_ACTIVITY).filter(last_activity__gte=cutoff)
