"""Bot→user Slack notification audit row (#963).

One row per ``notify_user(...)`` call, keyed by ``idempotency_key`` so a
retried turn (same session+turn) does not double-DM. The unique-key
collapse is the same shape as ``DailyDigestMessage``: at-least-once with
happy-path dedup, not exactly-once. Separate model from
``DailyDigestMessage`` because a bot→user notification has no notion of
a daily thread or a per-day root opener — it's a direct DM the agent
issues *to its own operator* outside the active CLI session.

Out of scope: posts made *on the user's behalf* to colleagues/customers
(those route through ``Replier`` / ``ReplyDispatch`` and the on-behalf
gates #960/#949). This model only audits notifications the bot sends
*to* the user.
"""

from typing import ClassVar

from django.db import models
from django.utils import timezone


class BotPing(models.Model):
    """One bot→user Slack notification (answer / question / info)."""

    class Kind(models.TextChoices):
        ANSWER = "answer", "Answer"
        QUESTION = "question", "Question"
        INFO = "info", "Info"

    class Status(models.TextChoices):
        SENT = "sent", "Sent"
        NOOP = "noop", "Noop (no backend)"
        FAILED = "failed", "Failed"

    idempotency_key = models.CharField(max_length=255, unique=True)
    kind = models.CharField(max_length=16, choices=Kind.choices)
    status = models.CharField(max_length=16, choices=Status.choices)
    text = models.TextField()
    channel_ref = models.CharField(max_length=255, blank=True)
    posted_ts = models.CharField(max_length=64, blank=True)
    permalink = models.URLField(max_length=512, blank=True)
    error_message = models.TextField(blank=True)
    posted_at = models.DateTimeField(default=timezone.now)

    class Meta:
        db_table = "teatree_bot_ping"
        ordering: ClassVar = ["-posted_at"]
        indexes: ClassVar = [
            models.Index(fields=["kind", "posted_at"]),
            models.Index(fields=["status", "posted_at"]),
        ]

    def __str__(self) -> str:
        return f"BotPing[{self.kind}/{self.status}] {self.idempotency_key}"
