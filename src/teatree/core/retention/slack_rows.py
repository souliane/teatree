"""Retention predicates for settled Slack DM rows and sent notification text."""

import datetime as dt

from django.db import models

from teatree.core.models import BotPing, PendingChatInjection

DM_RETENTION_DAYS = 30
SENT_TEXT_RETENTION_DAYS = 7
LONG_PAYLOAD_RETENTION_DAYS = 30


def pending_rows(cutoff: dt.datetime) -> models.QuerySet:
    return PendingChatInjection.objects.prunable(cutoff)


def sent_text_rows(cutoff: dt.datetime) -> models.QuerySet:
    long_payload_cutoff = cutoff - dt.timedelta(days=LONG_PAYLOAD_RETENTION_DAYS - SENT_TEXT_RETENTION_DAYS)
    return BotPing.objects.sent_text_prunable(cutoff).filter(posted_at__gte=long_payload_cutoff)


def blank_sent_text(rows: models.QuerySet) -> int:
    return rows.update(text="")
