"""Retention predicates for settled Slack DM rows and sent notification text."""

import datetime as dt

from django.db import models

from teatree.core.models import BotPing, PendingChatInjection
from teatree.core.slack_owner_registry import configured_slack_owner_ids

DM_RETENTION_DAYS = 30
SENT_TEXT_RETENTION_DAYS = 7
LONG_PAYLOAD_RETENTION_DAYS = 30


def pending_rows(cutoff: dt.datetime) -> models.QuerySet:
    old_rows = PendingChatInjection.objects.filter(received_at__lt=cutoff)
    foreign = models.Q(pk__in=[])
    for overlay, owner_id in configured_slack_owner_ids().items():
        foreign |= models.Q(overlay=overlay) & ~models.Q(user_id=owner_id)
    return old_rows.filter(models.Q(loop_replied_at__isnull=False) | foreign)


def sent_text_rows(cutoff: dt.datetime) -> models.QuerySet:
    long_payload_cutoff = cutoff - dt.timedelta(days=LONG_PAYLOAD_RETENTION_DAYS - SENT_TEXT_RETENTION_DAYS)
    return BotPing.objects.sent_text_prunable(cutoff).filter(posted_at__gte=long_payload_cutoff)


def blank_sent_text(rows: models.QuerySet) -> int:
    return rows.update(text="")
