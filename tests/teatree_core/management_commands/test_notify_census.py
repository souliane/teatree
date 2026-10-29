"""The DM census counts channel history and thread replies."""

import io
from unittest.mock import MagicMock, patch

import pytest
from django.core.management import call_command
from django.test import TestCase
from django.utils import timezone

from teatree.core.backend_protocols import MessagingReadRefusedError
from teatree.core.models import BotPing, Ticket, TicketDmPost, TicketDmThread


class TestNotifyCensus(TestCase):
    def test_reads_only_threads_with_activity_inside_the_window(self) -> None:
        recent_ts = f"{timezone.now().timestamp():.6f}"
        old_ts = f"{timezone.now().timestamp() - 86400 * 3:.6f}"
        backend = MagicMock()
        backend.open_dm.return_value = "D-USER"
        backend.fetch_channel_history_or_refuse.return_value = [
            {"ts": old_ts, "bot_id": "B1", "reply_count": 1, "latest_reply": old_ts},
            {"ts": recent_ts, "bot_id": "B1", "reply_count": 1, "latest_reply": recent_ts},
        ]
        backend.fetch_thread_replies.return_value = [{"ts": recent_ts, "bot_id": "B1"}]
        with patch("teatree.core.management.commands.notify.messaging_from_overlay", return_value=backend):
            call_command("notify", "census", "--days", "1", stdout=io.StringIO())

        backend.fetch_thread_replies.assert_called_once_with(channel="D-USER", thread_ts=recent_ts)

    def test_deleted_recent_thread_does_not_abort_census(self) -> None:
        recent_ts = f"{timezone.now().timestamp():.6f}"
        backend = MagicMock()
        backend.open_dm.return_value = "D-USER"
        backend.fetch_channel_history_or_refuse.return_value = [
            {"ts": recent_ts, "bot_id": "B1", "reply_count": 1, "latest_reply": recent_ts}
        ]
        backend.fetch_thread_replies.side_effect = MessagingReadRefusedError(
            "fetch_thread_replies", recent_ts, "thread_not_found"
        )
        out = io.StringIO()
        with patch("teatree.core.management.commands.notify.messaging_from_overlay", return_value=backend):
            call_command("notify", "census", "--days", "1", stdout=out)

        assert "1 message" in out.getvalue()

    def test_ledger_join_uses_delivery_channel(self) -> None:
        recent_ts = f"{timezone.now().timestamp():.6f}"
        backend = MagicMock()
        backend.open_dm.return_value = "D-DELIVERY"
        backend.fetch_channel_history_or_refuse.return_value = [{"ts": recent_ts, "bot_id": "B1"}]
        BotPing.objects.create(
            idempotency_key="watchdog:measured",
            kind=BotPing.Kind.INFO,
            status=BotPing.Status.SENT,
            channel_ref="D-DELIVERY",
            posted_ts=recent_ts,
        )
        out = io.StringIO()
        with (
            patch("teatree.core.management.commands.notify.messaging_from_overlay", return_value=backend),
            patch("teatree.core.notify_targets.resolve_user_channel", return_value="D-OTHER"),
        ):
            call_command("notify", "census", "--days", "1", stdout=out)

        assert "watchdog" in out.getvalue()
        backend.fetch_channel_history_or_refuse.assert_called_once_with(channel="D-DELIVERY", limit=200)

    def test_missing_root_timestamp_does_not_abort_truncation_check(self) -> None:
        recent_ts = f"{timezone.now().timestamp():.6f}"
        backend = MagicMock()
        backend.open_dm.return_value = "D-USER"
        backend.fetch_channel_history_or_refuse.return_value = [
            {"bot_id": "B1"},
            *({"ts": recent_ts, "bot_id": "B1"} for _ in range(199)),
        ]
        with patch("teatree.core.management.commands.notify.messaging_from_overlay", return_value=backend):
            call_command("notify", "census", "--days", "1", stdout=io.StringIO())

    def test_counts_bot_roots_and_replies_by_day_and_class(self) -> None:
        root_ts = f"{timezone.now().timestamp():.6f}"
        reply_ts = f"{timezone.now().timestamp() + 1:.6f}"
        backend = MagicMock()
        backend.open_dm.return_value = "D-USER"
        backend.fetch_channel_history_or_refuse.return_value = [
            {"ts": root_ts, "bot_id": "B1", "reply_count": 1},
            {"ts": f"{timezone.now().timestamp() + 2:.6f}", "user": "U_ME"},
        ]
        backend.fetch_thread_replies.return_value = [
            {"ts": root_ts, "bot_id": "B1"},
            {"ts": reply_ts, "bot_id": "B1"},
        ]
        BotPing.objects.create(
            idempotency_key="watchdog:compose-up-failed:incident",
            kind=BotPing.Kind.INFO,
            status=BotPing.Status.SENT,
            channel_ref="D-USER",
            posted_ts=reply_ts,
        )
        out = io.StringIO()
        with patch("teatree.core.management.commands.notify.messaging_from_overlay", return_value=backend):
            call_command("notify", "census", "--days", "1", stdout=out)
        rendered = out.getvalue()
        assert "watchdog" in rendered
        assert "unledgered" in rendered
        assert "2 message" in rendered
        backend.fetch_thread_replies.assert_called_once_with(channel="D-USER", thread_ts=root_ts)

    def test_ticket_post_has_its_own_class(self) -> None:
        ts = f"{timezone.now().timestamp():.6f}"
        ticket = Ticket.objects.create(short_description="Measured")
        thread = TicketDmThread.objects.create(ticket=ticket, channel="D-USER", root_ts=ts)
        TicketDmPost.objects.create(thread=thread, channel="D-USER", slack_ts=ts, generation=0)
        backend = MagicMock()
        backend.open_dm.return_value = "D-USER"
        backend.fetch_channel_history_or_refuse.return_value = [{"ts": ts, "bot_id": "B1"}]
        out = io.StringIO()
        with patch("teatree.core.management.commands.notify.messaging_from_overlay", return_value=backend):
            call_command("notify", "census", "--days", "1", stdout=out)
        assert "ticket-thread" in out.getvalue()

    def test_full_recent_history_flags_truncation(self) -> None:
        backend = MagicMock()
        backend.open_dm.return_value = "D-USER"
        backend.fetch_channel_history_or_refuse.return_value = [
            {"ts": f"{timezone.now().timestamp() + index:.6f}", "bot_id": "B1"} for index in range(200)
        ]
        out = io.StringIO()
        with patch("teatree.core.management.commands.notify.messaging_from_overlay", return_value=backend):
            call_command("notify", "census", "--days", "1", stdout=out)
        assert "window truncated" in out.getvalue()

    def test_refused_history_exits_nonzero(self) -> None:
        backend = MagicMock()
        backend.open_dm.return_value = "D-USER"
        backend.fetch_channel_history_or_refuse.side_effect = RuntimeError("read refused")
        with (
            patch("teatree.core.management.commands.notify.messaging_from_overlay", return_value=backend),
            pytest.raises(SystemExit) as exc,
        ):
            call_command("notify", "census", stdout=io.StringIO(), stderr=io.StringIO())
        assert exc.value.code == 1
