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
    # When the ``@engineers :pray:`` re-ping last fired (#1084 follow-up).
    # Null ⇒ never re-pinged; the scanner reads it to enforce no double-ping within
    # the current window. Claimed together with ``nag_count`` in one conditional UPDATE.
    last_nag_at = models.DateTimeField(null=True, blank=True)
    # How many times this MR has been re-asked. Drives the Fibonacci re-ask backoff:
    # each nag is due ``base_interval * fib(nag_count)`` after ``last_nag_at``, so the
    # interval widens as the count grows.
    nag_count = models.PositiveIntegerField(default=0)
    # Single-use idempotency stamp for the "now ready for review" reply posted into the
    # EXISTING Slack thread once the user lifts a pause reaction. Null ⇒ not yet resumed;
    # claimed by a conditional ``UPDATE ... WHERE resumed_at IS NULL``, which is why it
    # stays nullable with no default.
    resumed_at = models.DateTimeField(null=True, blank=True)
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
