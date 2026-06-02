"""Recorder for bot review-request posts (#1038).

When the bot posts an MR to the review channel,
it must call :func:`record_review_request_post` so the fibonacci nag
scanner can detect "already posted" MRs and escalate the cadence.

This module is the *write* surface. The *read* path lives in
:mod:`teatree.loop.scanners.review_nag`.

The seeder is idempotent on ``mr_url``: a retried post for the same MR
URL updates the existing row's thread reference rather than failing on
the unique-key collision. This handles the case where the bot's initial
post succeeded but the audit row write was lost to a transient failure.
"""

import logging

from django.db import transaction

from teatree.core.models import ReviewRequestPost

logger = logging.getLogger(__name__)


def record_review_request_post(
    *,
    mr_url: str,
    slack_channel_id: str,
    slack_thread_ts: str,
    bot_id: str = "",
) -> ReviewRequestPost:
    """Persist (or update) a ``ReviewRequestPost`` for *mr_url*.

    Idempotent on ``mr_url``: a re-post overwrites the channel and
    thread reference but leaves ``last_nag_step`` and ``done_at`` alone
    so the nag state machine is preserved across retries.
    """
    with transaction.atomic():
        post, created = ReviewRequestPost.objects.get_or_create(
            mr_url=mr_url,
            defaults={
                "slack_channel_id": slack_channel_id,
                "slack_thread_ts": slack_thread_ts,
                "bot_id": bot_id,
            },
        )
        if not created:
            post.slack_channel_id = slack_channel_id
            post.slack_thread_ts = slack_thread_ts
            if bot_id:
                post.bot_id = bot_id
            post.save(update_fields=["slack_channel_id", "slack_thread_ts", "bot_id"])
    logger.info(
        "review_request_post recorded (created=%s): mr_url=%s channel=%s thread=%s",
        created,
        mr_url,
        slack_channel_id,
        slack_thread_ts,
    )
    return post


__all__ = ["record_review_request_post"]
