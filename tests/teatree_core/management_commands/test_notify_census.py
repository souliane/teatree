"""The DM census counts channel history and thread replies."""

import io
from unittest.mock import MagicMock, patch

import pytest
from django.core.management import call_command
from django.test import TestCase
from django.utils import timezone

from teatree.core.models import BotPing, Ticket, TicketDmPost, TicketDmThread


class TestNotifyCensus(TestCase):
    def test_counts_bot_roots_and_replies_by_day_and_class(self) -> None:
        root_ts = f"{timezone.now().timestamp():.6f}"
        reply_ts = f"{timezone.now().timestamp() + 1:.6f}"
        backend = MagicMock()
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
        with (
            patch("teatree.core.management.commands.notify.messaging_from_overlay", return_value=backend),
            patch("teatree.core.management.commands.notify.resolve_user_channel", return_value="D-USER", create=True),
        ):
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
        backend.fetch_channel_history_or_refuse.return_value = [{"ts": ts, "bot_id": "B1"}]
        out = io.StringIO()
        with (
            patch("teatree.core.management.commands.notify.messaging_from_overlay", return_value=backend),
            patch("teatree.core.management.commands.notify.resolve_user_channel", return_value="D-USER"),
        ):
            call_command("notify", "census", "--days", "1", stdout=out)
        assert "ticket-thread" in out.getvalue()

    def test_full_recent_history_flags_truncation(self) -> None:
        backend = MagicMock()
        backend.fetch_channel_history_or_refuse.return_value = [
            {"ts": f"{timezone.now().timestamp() + index:.6f}", "bot_id": "B1"} for index in range(200)
        ]
        out = io.StringIO()
        with (
            patch("teatree.core.management.commands.notify.messaging_from_overlay", return_value=backend),
            patch("teatree.core.management.commands.notify.resolve_user_channel", return_value="D-USER"),
        ):
            call_command("notify", "census", "--days", "1", stdout=out)
        assert "window truncated" in out.getvalue()

    def test_refused_history_exits_nonzero(self) -> None:
        backend = MagicMock()
        backend.fetch_channel_history_or_refuse.side_effect = RuntimeError("read refused")
        with (
            patch("teatree.core.management.commands.notify.messaging_from_overlay", return_value=backend),
            patch("teatree.core.management.commands.notify.resolve_user_channel", return_value="D-USER", create=True),
            pytest.raises(SystemExit) as exc,
        ):
            call_command("notify", "census", stdout=io.StringIO(), stderr=io.StringIO())
        assert exc.value.code == 1
