"""Slack retention keeps unresolved work and notification dedup metadata."""

import datetime as dt

from django.test import TestCase
from django.utils import timezone

from teatree.core.models import BotPing, PendingChatInjection
from teatree.core.retention.prune import apply_retention, plan_retention


class SlackRowsRetentionTestCase(TestCase):
    def test_only_replied_old_dms_and_sent_old_text_are_pruned(self) -> None:
        now = timezone.now()
        old = now - dt.timedelta(days=31)
        old_text = now - dt.timedelta(days=8)
        replied = PendingChatInjection.record(channel="D1", slack_ts="1.0", text="done")
        unanswered = PendingChatInjection.record(channel="D1", slack_ts="2.0", text="open")
        PendingChatInjection.objects.filter(pk__in=[replied.pk, unanswered.pk]).update(received_at=old)
        PendingChatInjection.objects.filter(pk=replied.pk).update(loop_replied_at=old)
        sent = BotPing.objects.create(
            idempotency_key="sent-8d",
            kind=BotPing.Kind.INFO,
            status=BotPing.Status.SENT,
            text="private content",
            error_message="keep metadata",
            posted_at=old_text,
        )
        unsent = BotPing.objects.create(
            idempotency_key="failed-8d",
            kind=BotPing.Kind.INFO,
            status=BotPing.Status.FAILED,
            text="retry content",
            posted_at=old_text,
        )

        planned = plan_retention(now=now)
        assert {(lane.table, lane.rows, lane.compacted) for lane in planned.tables} >= {
            ("PendingChatInjection", 1, 0),
            ("BotPing (sent text)", 0, 1),
        }
        assert PendingChatInjection.objects.count() == 2
        assert BotPing.objects.get(pk=sent.pk).text == "private content"

        apply_retention(now=now)

        assert list(PendingChatInjection.objects.values_list("pk", flat=True)) == [unanswered.pk]
        sent.refresh_from_db()
        unsent.refresh_from_db()
        assert (sent.text, sent.error_message, sent.status) == ("", "keep metadata", BotPing.Status.SENT)
        assert unsent.text == "retry content"
