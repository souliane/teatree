"""Retention predicates for settled Slack DM rows and sent notification text."""

import datetime as dt

from django.db import models, transaction

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


def slack_row_counts(now: dt.datetime) -> dict[str, int]:
    return {
        "PendingChatInjection": pending_rows(now - dt.timedelta(days=DM_RETENTION_DAYS)).count(),
        "BotPing (sent text)": sent_text_rows(now - dt.timedelta(days=SENT_TEXT_RETENTION_DAYS)).count(),
    }


def prune_slack_rows(now: dt.datetime, batch_size: int = 200) -> dict[str, int]:
    if batch_size < 1:
        message = "batch_size must be positive"
        raise ValueError(message)
    result: dict[str, int] = {}
    lanes = (
        ("PendingChatInjection", pending_rows(now - dt.timedelta(days=DM_RETENTION_DAYS)), None),
        ("BotPing (sent text)", sent_text_rows(now - dt.timedelta(days=SENT_TEXT_RETENTION_DAYS)), blank_sent_text),
    )
    for name, rows, compact in lanes:
        result[name] = 0
        while batch := list(rows.order_by("pk").values_list("pk", flat=True)[:batch_size]):
            with transaction.atomic():
                selected = rows.filter(pk__in=batch)
                if compact is None:
                    acted = selected.count()
                    selected.delete()
                else:
                    acted = compact(selected)
            result[name] += acted
    return result
