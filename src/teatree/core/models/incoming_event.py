from datetime import datetime, timedelta
from typing import ClassVar

from django.db import models
from django.utils import timezone

from teatree.core.managers import IncomingEventManager

#: Attempts a poisoned event gets before it is dead-lettered (#673). Each
#: failed drain records the error and schedules an exponential-backoff retry;
#: past this many attempts the event stops re-firing and surfaces for triage
#: instead of blocking the queue forever.
MAX_INGEST_ATTEMPTS = 5

#: Backoff base — the delay before the first retry doubles each attempt,
#: capped so a persistently-failing event never schedules its retry beyond
#: a bounded horizon.
_RETRY_BASE = timedelta(seconds=30)
_RETRY_CAP = timedelta(hours=1)


class IncomingEvent(models.Model):
    """One inbound webhook payload from an external platform.

    The single ingestion record every later phase of issue #654 reads
    from: the intent classifier, the dispatcher branch, and the reply
    transport. Stored ahead of the platform receivers so the persistence
    layer is stable before any HMAC-verifying view is wired up.
    """

    class Source(models.TextChoices):
        SLACK = "slack", "Slack"
        GITLAB = "gitlab", "GitLab"
        GITHUB = "github", "GitHub"
        NOTION = "notion", "Notion"
        CI = "ci", "CI"

    source = models.CharField(max_length=16, choices=Source.choices)
    actor = models.CharField(max_length=255, blank=True)
    channel_ref = models.CharField(max_length=255, blank=True)
    thread_ref = models.CharField(max_length=255, blank=True)
    parent_ts = models.CharField(max_length=255, blank=True)
    parent_text = models.TextField(blank=True)
    body = models.TextField(blank=True)
    payload_json = models.JSONField(default=dict, blank=True)
    received_at = models.DateTimeField(default=timezone.now)
    processed_at = models.DateTimeField(null=True, blank=True)
    idempotency_key = models.CharField(max_length=255, unique=True)
    attempts = models.PositiveIntegerField(default=0)
    last_error = models.TextField(blank=True)
    next_retry_at = models.DateTimeField(null=True, blank=True)
    dead_lettered_at = models.DateTimeField(null=True, blank=True)

    objects = IncomingEventManager()

    class Meta:
        db_table = "teatree_incoming_event"
        ordering: ClassVar = ["-received_at"]
        indexes: ClassVar = [
            models.Index(fields=["source", "received_at"]),
            models.Index(fields=["processed_at"]),
            models.Index(fields=["dead_lettered_at"]),
        ]

    def __str__(self) -> str:
        return f"{self.source}:{self.idempotency_key}"

    @property
    def is_thread_reply(self) -> bool:
        """True iff this event is a reply under a parent message (#2230)."""
        return bool(self.parent_ts)

    @property
    def is_dead_lettered(self) -> bool:
        return self.dead_lettered_at is not None

    def mark_processed(self) -> None:
        self.processed_at = timezone.now()
        self.save(update_fields=["processed_at"])

    def record_failure(
        self,
        error: str,
        *,
        max_attempts: int = MAX_INGEST_ATTEMPTS,
        now: datetime | None = None,
    ) -> bool:
        """Record a failed drain attempt; return True iff this dead-letters the event (#673).

        A drain that raises must never silently drop the event (``mark_processed``
        would hide the poison) nor block the queue (an unbounded re-fire loop
        starves every following event). Instead each failure bumps ``attempts``,
        stores the truncated ``error``, and either schedules an
        exponential-backoff retry (``next_retry_at``) or — once ``attempts``
        reaches ``max_attempts`` — dead-letters the event (``dead_lettered_at``)
        so it stops re-firing and surfaces for triage. The event is left
        ``processed_at is None`` on a retry so the next due tick re-drains it;
        dead-lettering does not set ``processed_at`` either, because the
        ``unprocessed`` query excludes dead-lettered rows on its own.
        """
        moment = now or timezone.now()
        self.attempts += 1
        self.last_error = error[:2000]
        if self.attempts >= max_attempts:
            self.dead_lettered_at = moment
            self.next_retry_at = None
            self.save(update_fields=["attempts", "last_error", "dead_lettered_at", "next_retry_at"])
            return True
        self.next_retry_at = moment + self._backoff(self.attempts)
        self.save(update_fields=["attempts", "last_error", "next_retry_at"])
        return False

    @staticmethod
    def _backoff(attempts: int) -> timedelta:
        """Exponential backoff for retry *attempts*, capped at ``_RETRY_CAP``."""
        delay = _RETRY_BASE * (2 ** (attempts - 1))
        return min(delay, _RETRY_CAP)

    def record_parent_text(self, text: str) -> None:
        """Persist the resolved parent-message *text* (single-field write)."""
        self.parent_text = text
        self.save(update_fields=["parent_text"])
