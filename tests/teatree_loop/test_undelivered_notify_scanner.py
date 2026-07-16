"""Behaviour tests for :class:`UndeliveredNotifyScanner` (#173)."""

from unittest.mock import patch

from django.db import OperationalError
from django.test import TestCase

from teatree.core.models import BotPing
from teatree.loop.scanners.undelivered_notify import UndeliveredNotifyScanner


class TestUndeliveredNotifyScanner(TestCase):
    def test_no_signal_when_nothing_redelivered(self) -> None:
        with patch(
            "teatree.core.notify.drain_undelivered_notifies",
            return_value=(0, 0),
        ):
            assert UndeliveredNotifyScanner().scan() == []

    def test_emits_signal_when_dms_redelivered(self) -> None:
        with patch(
            "teatree.core.notify.drain_undelivered_notifies",
            return_value=(2, 3),
        ):
            signals = UndeliveredNotifyScanner().scan()
        assert len(signals) == 1
        assert signals[0].kind == "notify.redelivered"
        assert signals[0].payload == {"delivered": 2, "total": 3}

    def test_db_unavailable_is_silent_noop(self) -> None:
        with patch(
            "teatree.core.notify.drain_undelivered_notifies",
            side_effect=OperationalError("no such table: teatree_bot_ping"),
        ):
            assert UndeliveredNotifyScanner().scan() == []

    def test_unexpected_error_never_raises(self) -> None:
        with patch(
            "teatree.core.notify.drain_undelivered_notifies",
            side_effect=RuntimeError("boom"),
        ):
            assert UndeliveredNotifyScanner().scan() == []

    def test_redelivers_parked_info_row_end_to_end(self) -> None:
        BotPing.objects.create(
            idempotency_key="subagent-strand",
            kind=BotPing.Kind.INFO,
            status=BotPing.Status.NOOP,
            text="sub-agent finished",
            audience="owner_delivery",  # only owner-audience rows re-deliver
        )

        class _FakeBackend:
            def open_dm(self, _user_id: str) -> str:
                return "D-USER"

            def post_message(self, *, channel: str, text: str, thread_ts: str) -> dict[str, object]:
                del channel, text, thread_ts
                return {"ok": True, "ts": "1700000000.000000"}

            def get_permalink(self, *, channel: str, ts: str) -> str:
                del channel, ts
                return "https://acme.slack.com/archives/D-USER/p1700000000000000"

        with (
            patch("teatree.core.notify.messaging_from_overlay", return_value=_FakeBackend()),
            patch("teatree.core.notify.resolve_user_id", return_value="U_ME"),
        ):
            signals = UndeliveredNotifyScanner().scan()

        assert [s.kind for s in signals] == ["notify.redelivered"]
        assert BotPing.objects.get(idempotency_key="subagent-strand").status == BotPing.Status.SENT
