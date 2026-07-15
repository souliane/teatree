"""Audit row for bot review-request posts in the review channel (#1038).

One row per MR posted to the review channel so the fibonacci nag
scanner can detect "already posted" MRs and escalate the cadence
(+1/+2/+3/+5 days) without re-discovering the original message.

Separate from ``ticket.extra["prs"]["<url>"]["review_permalink"]``
(populated by the Slack review sync). That field is read by the sync
on every poll; this model is the *write* surface for the bot's
review-request post and the nag-step state machine.
"""

from typing import ClassVar

from django.db import models
from django.utils import timezone


class ReviewRequestPost(models.Model):
    """One review-channel post tracked for fibonacci nag escalation."""

    mr_url = models.URLField(max_length=512, unique=True)
    slack_channel_id = models.CharField(max_length=64)
    slack_thread_ts = models.CharField(max_length=64)
    bot_id = models.CharField(max_length=64, blank=True)
    last_nag_step = models.PositiveSmallIntegerField(default=0)
    # When the 2-day ``@engineers :pray:`` re-ping last fired (#1084 follow-up).
    # Null ⇒ never re-pinged. The nag scanner uses it to enforce "no double-ping
    # within 2 days" independently of the retired fibonacci ``last_nag_step``.
    last_nag_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(default=timezone.now)
    done_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        db_table = "teatree_review_request_post"
        ordering: ClassVar = ["-created_at"]
        indexes: ClassVar = [
            models.Index(fields=["last_nag_step", "created_at"]),
            models.Index(fields=["done_at"]),
        ]

    def __str__(self) -> str:
        return f"ReviewRequestPost[{self.mr_url} step={self.last_nag_step}]"
