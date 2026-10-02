"""Inbound-event queue predicates — the drain, retry, prune and dead-letter views.

Split out of :mod:`teatree.core.managers` (which holds the ticket / worktree /
task lifecycle) because the inbound queue is a separate concern: it answers
"what is still owed a drain" rather than "where is this unit of work".
"""

from datetime import datetime
from typing import TYPE_CHECKING, cast

from django.apps import apps
from django.db import models
from django.db.models import Q
from django.utils import timezone

if TYPE_CHECKING:
    from teatree.core.models.incoming_event import IncomingEvent
    from teatree.core.models.reply_dispatch import ReplyDispatch

__all__ = ["IncomingEventQuerySet", "ReplyDispatchQuerySet"]

#: Neither drained nor poisoned — the in-flight boundary both the drain and the
#: pruner key on, so they can never disagree about what is still owed.
_UNSETTLED = Q(processed_at__isnull=True, dead_lettered_at__isnull=True)


class IncomingEventQuerySet(models.QuerySet):
    def unprocessed(self, now: datetime | None = None) -> models.QuerySet:
        """Events still awaiting a drain: un-processed, not dead-lettered, and due (#673).

        A failed drain (:meth:`IncomingEvent.record_failure`) leaves the event
        un-processed but stamps a backoff ``next_retry_at`` and, past the attempt
        cap, a ``dead_lettered_at``. Excluding both here is what lets the scanner
        retry a transient failure without re-firing it every tick and drop a
        dead-lettered poison out of the queue rather than block behind it.
        """
        moment = now or timezone.now()
        return self.filter(_UNSETTLED).filter(Q(next_retry_at__isnull=True) | Q(next_retry_at__lte=moment))

    def prunable(self, cutoff: datetime) -> models.QuerySet:
        # Settled events before *cutoff*, safe to delete (#3693). Excludes the _UNSETTLED
        # boundary itself (drained/dead-lettered) — NOT unprocessed(): a not-yet-due backoff
        # row is still in-flight and must never be pruned however old.
        return self.exclude(_UNSETTLED).filter(received_at__lt=cutoff)

    def dead_lettered(self) -> models.QuerySet:
        """Poisoned events that exhausted their retries — the dead-letter view (#673)."""
        return self.filter(dead_lettered_at__isnull=False).order_by("-dead_lettered_at", "-pk")

    def active_dm_thread(self, *, channel: str) -> str:
        incoming_event_model = cast("type[IncomingEvent]", apps.get_model("core", "IncomingEvent"))

        if not channel:
            return ""
        latest = (
            self.filter(source=incoming_event_model.Source.SLACK, channel_ref=channel)
            .order_by("-received_at", "-pk")
            .values_list("thread_ref", flat=True)
            .first()
        )
        return latest or ""


class ReplyDispatchQuerySet(models.QuerySet):
    def due_for_retry(self, now: datetime | None = None) -> models.QuerySet:
        reply_dispatch_model = cast("type[ReplyDispatch]", apps.get_model("core", "ReplyDispatch"))

        moment = now or timezone.now()
        return (
            self.filter(status=reply_dispatch_model.Status.FAILED)
            .exclude(action_name="dead_letter_alert")
            .filter(models.Q(next_retry_at__isnull=True) | models.Q(next_retry_at__lte=moment))
            .order_by("next_retry_at", "pk")
        )
