"""Inbound-event queue predicates — the drain, retry, prune and dead-letter views.

Split out of :mod:`teatree.core.managers` (which holds the ticket / worktree /
task lifecycle) because the inbound queue is a separate concern: it answers
"what is still owed a drain" rather than "where is this unit of work".
"""

from collections.abc import Iterable
from datetime import datetime, timedelta

from django.db import models
from django.db.models import F, Q, Window
from django.db.models.functions import RowNumber
from django.utils import timezone

from teatree.utils.forge import FORGES

__all__ = ["HANDLING_LEASE", "IncomingEventQuerySet", "ReplyDispatchQuerySet"]

HANDLING_LEASE = timedelta(seconds=120)

#: Neither drained nor poisoned — the in-flight boundary both the drain and the
#: pruner key on, so they can never disagree about what is still owed.
_UNSETTLED = Q(processed_at__isnull=True, dead_lettered_at__isnull=True)
_SETTLED_FORGE = Q(source__in=FORGES, processed_at__isnull=False, dead_lettered_at__isnull=True)


class IncomingEventQuerySet(models.QuerySet):
    @staticmethod
    def is_settled_forge(source: str, *, settled: bool = True, dead_lettered: bool) -> bool:
        return source in FORGES and settled and not dead_lettered

    def forge_identity_rows(self, cutoff: datetime) -> models.QuerySet:
        return self.prunable(cutoff).filter(_SETTLED_FORGE)

    def non_identity_prunable_rows(self, cutoff: datetime) -> models.QuerySet:
        return self.prunable(cutoff).exclude(_SETTLED_FORGE)

    def webhooks_verified_for(self, source: str, repo_key: str, event_name: str, window: timedelta) -> bool:
        if not repo_key or not event_name:
            return False
        return self.filter(
            source=source,
            repo_key=repo_key,
            event_name=event_name,
            origin__in=("webhook", "relay"),
            received_at__gte=timezone.now() - window,
        ).exists()

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

    def drain_batch(self, limit: int, *, budgeted: Iterable[tuple[str, str]], per_key: int) -> models.QuerySet:
        # A preset-deferred row keeps next_retry_at None, so uncapped its key would fill every oldest-first batch.
        due = self.unprocessed().order_by("received_at", "pk")
        capped = Q()
        for source, event_name in budgeted:
            capped |= Q(source=source, event_name=event_name)
        if not capped:
            return due[:limit]
        rank = Window(RowNumber(), partition_by=[F("source"), F("event_name")], order_by=[F("received_at"), F("pk")])
        return due.annotate(key_rank=rank).filter(~capped | Q(key_rank__lte=per_key))[:limit]

    def claim_for_handling(self, pk: int, now: datetime) -> bool:
        # One conditional UPDATE: the control DB is SQLite, which has no row locks to select_for_update.
        return self.unprocessed(now).filter(pk=pk).update(next_retry_at=now + HANDLING_LEASE) == 1

    def prunable(self, cutoff: datetime) -> models.QuerySet:
        # Settled events before *cutoff*, safe to delete (#3693). Excludes the _UNSETTLED
        # boundary itself (drained/dead-lettered) — NOT unprocessed(): a not-yet-due backoff
        # row is still in-flight and must never be pruned however old.
        return self.exclude(_UNSETTLED).filter(received_at__lt=cutoff)

    def dead_lettered(self) -> models.QuerySet:
        """Poisoned events that exhausted their retries — the dead-letter view (#673)."""
        return self.filter(dead_lettered_at__isnull=False).order_by("-dead_lettered_at", "-pk")


class ReplyDispatchQuerySet(models.QuerySet):
    """QuerySet retained for the ReplyDispatch manager."""
