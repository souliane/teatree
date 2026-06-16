"""Tests for the :class:`SlackDmInboundScanner` (#1014).

The Slack inbound bridge — WS1 + WS4. The scanner polls the overlay's
``MessagingBackend.fetch_dms`` for new user messages and inserts one
:class:`PendingChatInjection` row per unique Slack ``ts``. The Slack
backend itself filters bot-authored messages out of ``fetch_dms``; this
scanner trusts that contract and only adds idempotent persistence.
"""

from dataclasses import dataclass, field

import pytest

from teatree.core.models import PendingChatInjection
from teatree.loop.scanners.slack_dm_inbound import SlackDmInboundScanner
from teatree.types import RawAPIDict

# ast-grep-ignore: ac-django-no-pytest-django-db
pytestmark = pytest.mark.django_db


@dataclass
class FakeMessaging:
    """In-memory MessagingBackend for scanner tests."""

    dms: list[RawAPIDict] = field(default_factory=list)
    fetch_dms_calls: list[str] = field(default_factory=list)

    def fetch_mentions(self, *, since: str = "") -> list[RawAPIDict]:
        _ = since
        return []

    def fetch_dms(self, *, since: str = "") -> list[RawAPIDict]:
        self.fetch_dms_calls.append(since)
        return self.dms

    def post_message(self, *, channel: str, text: str, thread_ts: str = "") -> RawAPIDict:
        _ = (channel, text, thread_ts)
        return {}

    def post_reply(self, *, channel: str, ts: str, text: str) -> RawAPIDict:
        _ = (channel, ts, text)
        return {}

    def open_dm(self, user_id: str) -> str:
        _ = user_id
        return "D0DEMOTEAM1"

    def get_permalink(self, *, channel: str, ts: str) -> str:
        _ = (channel, ts)
        return ""

    def react(self, *, channel: str, ts: str, emoji: str) -> RawAPIDict:
        _ = (channel, ts, emoji)
        return {}

    def resolve_user_id(self, handle: str) -> str:
        _ = handle
        return ""

    def auth_test(self) -> RawAPIDict:
        # The scanner self-filter (#1346) probes this once to learn the
        # bot's own user / bot ids. Returning a fully-resolved identity
        # keeps these legacy tests focused on persistence and
        # idempotency behaviour rather than the self-filter path.
        return {"ok": True, "user_id": "U_BOT_SELF", "bot_id": "B_BOT_SELF"}


class TestScan:
    def test_one_user_message_creates_one_row(self) -> None:
        backend = FakeMessaging(
            dms=[
                {
                    "ts": "1700000000.0001",
                    "user": "U0DEMOUSER1",
                    "channel": "D0DEMOTEAM1",
                    "text": "Hello, agent",
                }
            ]
        )
        signals = SlackDmInboundScanner(backend=backend, overlay="demo").scan()

        assert [s.kind for s in signals] == ["slack.user_reply"]
        rows = list(PendingChatInjection.objects.all())
        assert len(rows) == 1
        assert rows[0].text == "Hello, agent"
        assert rows[0].slack_ts == "1700000000.0001"
        assert rows[0].user_id == "U0DEMOUSER1"
        assert rows[0].channel == "D0DEMOTEAM1"
        assert rows[0].overlay == "demo"
        assert rows[0].is_pending is True

    def test_empty_dm_queue_yields_no_signals_or_rows(self) -> None:
        backend = FakeMessaging(dms=[])
        signals = SlackDmInboundScanner(backend=backend, overlay="demo").scan()

        assert signals == []
        assert PendingChatInjection.objects.count() == 0

    def test_same_ts_seen_twice_creates_one_row_no_duplicate(self) -> None:
        """Idempotency: scanner can over-poll without creating duplicates."""
        backend = FakeMessaging(
            dms=[
                {"ts": "1.0", "user": "U0DEMOUSER1", "channel": "D0DEMOTEAM1", "text": "First"},
            ]
        )
        scanner = SlackDmInboundScanner(backend=backend, overlay="demo")

        scanner.scan()
        second_signals = scanner.scan()

        assert PendingChatInjection.objects.count() == 1
        assert second_signals == []

    def test_event_ts_fallback_when_ts_absent(self) -> None:
        backend = FakeMessaging(
            dms=[{"event_ts": "5.0", "user": "U0DEMOUSER1", "channel": "D0DEMOTEAM1", "text": "no ts"}]
        )
        signals = SlackDmInboundScanner(backend=backend, overlay="demo").scan()

        assert len(signals) == 1
        assert signals[0].payload["ts"] == "5.0"
        assert PendingChatInjection.objects.filter(slack_ts="5.0").count() == 1

    def test_messages_without_ts_or_text_are_skipped(self) -> None:
        backend = FakeMessaging(
            dms=[
                {"user": "U0DEMOUSER1", "channel": "D", "text": "no-ts"},
                {"ts": "1.0", "user": "U0DEMOUSER1", "channel": "D", "text": ""},
                {"ts": "2.0", "user": "U0DEMOUSER1", "channel": "D", "text": "good"},
            ]
        )
        signals = SlackDmInboundScanner(backend=backend, overlay="demo").scan()

        assert len(signals) == 1
        assert PendingChatInjection.objects.count() == 1
        assert PendingChatInjection.objects.first().text == "good"

    def test_signal_payload_contains_channel_and_text(self) -> None:
        backend = FakeMessaging(
            dms=[
                {"ts": "1.0", "user": "U0DEMOUSER1", "channel": "D0DEMOTEAM1", "text": "ping"},
            ]
        )
        signals = SlackDmInboundScanner(backend=backend, overlay="demo").scan()

        assert signals[0].payload == {
            "ts": "1.0",
            "channel": "D0DEMOTEAM1",
            "user_id": "U0DEMOUSER1",
            "text": "ping",
            "overlay": "demo",
        }
        assert "ping" in signals[0].summary

    def test_multiple_user_messages_create_multiple_rows(self) -> None:
        backend = FakeMessaging(
            dms=[
                {"ts": "1.0", "user": "U0DEMOUSER1", "channel": "D", "text": "first"},
                {"ts": "2.0", "user": "U0DEMOUSER1", "channel": "D", "text": "second"},
            ]
        )
        SlackDmInboundScanner(backend=backend, overlay="demo").scan()

        rows = list(PendingChatInjection.objects.order_by("slack_ts"))
        assert [row.slack_ts for row in rows] == ["1.0", "2.0"]
        assert [row.text for row in rows] == ["first", "second"]

    def test_default_overlay_is_empty_string(self) -> None:
        """Single-overlay deployments don't need to pass an overlay tag."""
        backend = FakeMessaging(
            dms=[{"ts": "1.0", "user": "U", "channel": "D", "text": "hi"}],
        )
        SlackDmInboundScanner(backend=backend).scan()

        assert PendingChatInjection.objects.first().overlay == ""

    def test_scanner_name_includes_overlay_for_logging(self) -> None:
        backend = FakeMessaging()
        scanner = SlackDmInboundScanner(backend=backend, overlay="demo")
        assert "slack_dm_inbound" in scanner.name
