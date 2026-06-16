"""Regression: scanner must drop bot's own outbound DMs (#1346).

The Slack inbound bridge enqueues a ``PendingChatInjection`` row for every
DM the bot's IM channel surfaces. The Socket Mode receiver only filters
``subtype=bot_message`` — but Slack delivers the bot's own outbound posts
as regular ``message`` events whose ``user`` matches the bot's user id and
whose ``bot_id`` matches the bot's bot id. Without a self-filter at the
scanner the bot's outbound DMs are persisted, the UserPromptSubmit hook
injects them as "user replies", and the reactive Slack-answer cycle spawns
``t3:answerer`` sub-agents that try to answer the bot's own message.

The filter must apply at the lowest common helper so BOTH downstream
consumers — the UserPromptSubmit ``handle_inject_pending_chat`` and the
reactive ``run_slack_answer_cycle`` — inherit it. Filtering at
``SlackDmInboundScanner.scan()`` (the write side) achieves that: rows that
fail the filter never reach the DB.

Fail-closed: when the bot's own identity cannot be resolved (network down
at startup, auth.test returns ok:false), the scanner refuses to enqueue
any row that turn — better silent than spam-spawning answerer sub-agents
against unfiltered traffic.
"""

from dataclasses import dataclass, field

import pytest

from teatree.core.models import PendingChatInjection
from teatree.loop.scanners.slack_dm_inbound import SlackDmInboundScanner
from teatree.types import RawAPIDict

# ast-grep-ignore: ac-django-no-pytest-django-db
pytestmark = pytest.mark.django_db

_OWN_USER_ID = "U_BOT_SELF"
_OWN_BOT_ID = "B_BOT_SELF"
_USER_ID = "U_HUMAN"
_CHANNEL = "D0DEMOCLNT1"


@dataclass
class FakeMessagingWithAuth:
    """MessagingBackend whose ``auth_test`` returns the bot's own ids."""

    dms: list[RawAPIDict] = field(default_factory=list)
    auth_response: RawAPIDict = field(
        default_factory=lambda: {"ok": True, "user_id": _OWN_USER_ID, "bot_id": _OWN_BOT_ID},
    )
    auth_test_calls: int = 0

    def fetch_mentions(self, *, since: str = "") -> list[RawAPIDict]:
        _ = since
        return []

    def fetch_dms(self, *, since: str = "") -> list[RawAPIDict]:
        _ = since
        return self.dms

    def fetch_reactions(self, *, since: str = "") -> list[RawAPIDict]:
        _ = since
        return []

    def fetch_message(self, *, channel: str, ts: str) -> RawAPIDict:
        _ = (channel, ts)
        return {}

    def fetch_channel_history(self, *, channel: str, limit: int = 50) -> list[RawAPIDict]:
        _ = (channel, limit)
        return []

    def post_message(self, *, channel: str, text: str, thread_ts: str = "") -> RawAPIDict:
        _ = (channel, text, thread_ts)
        return {}

    def post_reply(self, *, channel: str, ts: str, text: str) -> RawAPIDict:
        _ = (channel, ts, text)
        return {}

    def open_dm(self, user_id: str) -> str:
        _ = user_id
        return _CHANNEL

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
        self.auth_test_calls += 1
        return self.auth_response


class TestSelfFilter:
    """The scanner must drop messages authored by the bot itself (#1346)."""

    def test_drops_bot_outbound_when_user_matches_own_user_id(self) -> None:
        backend = FakeMessagingWithAuth(
            dms=[
                {
                    "ts": "1779823899.073799",
                    "user": _OWN_USER_ID,
                    "channel": _CHANNEL,
                    "text": "teatree#1283 already fixed on main",
                },
                {
                    "ts": "1779823900.000000",
                    "user": _USER_ID,
                    "channel": _CHANNEL,
                    "text": "actual user reply",
                },
            ]
        )

        signals = SlackDmInboundScanner(backend=backend, overlay="demo").scan()

        rows = list(PendingChatInjection.objects.all())
        assert len(rows) == 1, f"expected only the user message to persist, got {[r.text for r in rows]}"
        assert rows[0].text == "actual user reply"
        assert rows[0].user_id == _USER_ID
        assert [s.payload["ts"] for s in signals] == ["1779823900.000000"]

    def test_drops_bot_outbound_when_bot_id_matches_own_bot_id(self) -> None:
        """Bot-style messages may carry only ``bot_id`` (no ``user``)."""
        backend = FakeMessagingWithAuth(
            dms=[
                {
                    "ts": "1.0",
                    "bot_id": _OWN_BOT_ID,
                    "channel": _CHANNEL,
                    "text": "bot post (no user field)",
                },
                {
                    "ts": "2.0",
                    "user": _USER_ID,
                    "channel": _CHANNEL,
                    "text": "real user message",
                },
            ]
        )

        SlackDmInboundScanner(backend=backend, overlay="demo").scan()

        rows = list(PendingChatInjection.objects.all())
        assert len(rows) == 1
        assert rows[0].text == "real user message"

    def test_user_messages_still_enqueued(self) -> None:
        """The filter must not be over-broad — genuine user DMs pass through."""
        backend = FakeMessagingWithAuth(
            dms=[
                {"ts": "1.0", "user": _USER_ID, "channel": _CHANNEL, "text": "hi"},
                {"ts": "2.0", "user": _USER_ID, "channel": _CHANNEL, "text": "more"},
            ]
        )

        SlackDmInboundScanner(backend=backend, overlay="demo").scan()

        assert PendingChatInjection.objects.count() == 2

    def test_fail_closed_when_auth_test_returns_not_ok(self) -> None:
        """Identity unknown → no rows enqueued (fail-closed: silent > spam)."""
        backend = FakeMessagingWithAuth(
            dms=[
                {"ts": "1.0", "user": _USER_ID, "channel": _CHANNEL, "text": "real user message"},
            ],
            auth_response={"ok": False, "error": "invalid_auth"},
        )

        signals = SlackDmInboundScanner(backend=backend, overlay="demo").scan()

        assert signals == []
        assert PendingChatInjection.objects.count() == 0

    def test_fail_closed_when_auth_test_returns_empty(self) -> None:
        """Empty auth.test response (no bot token configured) → no rows."""
        backend = FakeMessagingWithAuth(
            dms=[
                {"ts": "1.0", "user": _USER_ID, "channel": _CHANNEL, "text": "user msg"},
            ],
            auth_response={},
        )

        signals = SlackDmInboundScanner(backend=backend, overlay="demo").scan()

        assert signals == []
        assert PendingChatInjection.objects.count() == 0

    def test_fail_closed_when_auth_test_raises(self) -> None:
        """A transport failure in auth.test → no rows (fail-closed)."""
        transport_error = RuntimeError("transport down")

        @dataclass
        class RaisingBackend(FakeMessagingWithAuth):
            def auth_test(self) -> RawAPIDict:
                raise transport_error

        backend = RaisingBackend(
            dms=[
                {"ts": "1.0", "user": _USER_ID, "channel": _CHANNEL, "text": "user msg"},
            ],
        )

        signals = SlackDmInboundScanner(backend=backend, overlay="demo").scan()

        assert signals == []
        assert PendingChatInjection.objects.count() == 0

    def test_auth_test_resolved_once_per_scanner(self) -> None:
        """Identity is cached on the scanner — repeated scans don't re-probe auth.test."""
        backend = FakeMessagingWithAuth(
            dms=[
                {"ts": "1.0", "user": _USER_ID, "channel": _CHANNEL, "text": "first"},
            ]
        )
        scanner = SlackDmInboundScanner(backend=backend, overlay="demo")

        scanner.scan()
        scanner.scan()
        scanner.scan()

        assert backend.auth_test_calls == 1
