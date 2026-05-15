from typing import ClassVar

from django.db import models
from django.utils import timezone


class DailyDigestThread(models.Model):
    """One rolling DM thread per day (#654 phase 8, #672).

    All user-facing comms for a given digest day — AskUserQuestion
    mirrors, error escalations, "PR merged" notices, the end-of-day
    recap — land as replies under this thread's root message. The day
    rolls at 08:00 local time (configured ``TIME_ZONE``; hour via
    ``TEATREE_DAILY_DIGEST_ROLL_HOUR``). A new row (new Slack thread)
    opens the first time anything posts in a new window; the previous
    window's thread is closed by its end-of-day recap.
    """

    date = models.DateField(unique=True)
    channel_ref = models.CharField(max_length=255)
    root_ts = models.CharField(max_length=64)
    opened_at = models.DateTimeField(default=timezone.now)
    closed_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        db_table = "teatree_daily_digest_thread"
        ordering: ClassVar = ["-date"]

    def __str__(self) -> str:
        state = "closed" if self.closed_at else "open"
        return f"digest {self.date} ({state})"


class DailyDigestMessage(models.Model):
    """One posted message in a daily digest thread.

    The unique ``idempotency_key`` makes a retried post a no-op (the
    Slack reply is not sent twice). Also the per-thread message ledger.
    """

    thread = models.ForeignKey(
        DailyDigestThread,
        on_delete=models.CASCADE,
        related_name="messages",
    )
    idempotency_key = models.CharField(max_length=255, unique=True)
    posted_at = models.DateTimeField(default=timezone.now)

    class Meta:
        db_table = "teatree_daily_digest_message"
        ordering: ClassVar = ["posted_at"]

    def __str__(self) -> str:
        return f"{self.idempotency_key}@{self.thread.pk}"
