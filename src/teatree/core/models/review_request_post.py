"""Audit row for bot review-request posts in the review channel (#1038).

One row per MR posted to the review channel so the nag scanner can detect
"already posted" MRs and re-ping without re-discovering the original message.

Separate from ``ticket.extra["prs"]["<url>"]["review_permalink"]``
(populated by the Slack review sync). That field is read by the sync
on every poll; this model is the *write* surface for the bot's
review-request post.
"""

from typing import ClassVar

from django.db import models
from django.utils import timezone


class ReviewRequestPost(models.Model):
    """One review-channel post tracked for nag re-pings."""

    mr_url = models.URLField(max_length=512, unique=True)
    slack_channel_id = models.CharField(max_length=64)
    slack_thread_ts = models.CharField(max_length=64)
    bot_id = models.CharField(max_length=64, blank=True)
    # When the 2-day ``@engineers :pray:`` re-ping last fired (#1084 follow-up).
    # Null ⇒ never re-pinged; the scanner reads it to enforce no double-ping within 2 days.
    last_nag_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(default=timezone.now)
    done_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        db_table = "teatree_review_request_post"
        ordering: ClassVar = ["-created_at"]
        indexes: ClassVar = [
            models.Index(fields=["created_at"]),
            models.Index(fields=["done_at"]),
        ]

    def __str__(self) -> str:
        return f"ReviewRequestPost[{self.mr_url}]"
