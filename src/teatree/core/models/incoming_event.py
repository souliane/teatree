from typing import ClassVar

from django.db import models
from django.utils import timezone

from teatree.core.managers import IncomingEventManager


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
    body = models.TextField(blank=True)
    payload_json = models.JSONField(default=dict, blank=True)
    received_at = models.DateTimeField(default=timezone.now)
    processed_at = models.DateTimeField(null=True, blank=True)
    idempotency_key = models.CharField(max_length=255, unique=True)

    objects = IncomingEventManager()

    class Meta:
        db_table = "teatree_incoming_event"
        ordering: ClassVar = ["-received_at"]
        indexes: ClassVar = [
            models.Index(fields=["source", "received_at"]),
            models.Index(fields=["processed_at"]),
        ]

    def __str__(self) -> str:
        return f"{self.source}:{self.idempotency_key}"

    def mark_processed(self) -> None:
        self.processed_at = timezone.now()
        self.save(update_fields=["processed_at"])
