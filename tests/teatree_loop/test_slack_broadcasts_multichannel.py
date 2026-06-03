"""Multi-channel review broadcast routing tests (#1295 capability A).

Asserts that when the overlay configures multiple review broadcast channels
via :meth:`OverlayConfig.get_review_broadcast_channels`, posting the same
MR URL to all of them produces one :class:`ScannedBroadcast` row and one
review-intent dispatch per ``(channel, slack_ts)`` — the scanner fans out
without dedup-by-MR. No ``:eyes:`` claim reaction is posted at discovery
(#113/#86); the claim reaction belongs to review-DONE.
"""

from dataclasses import dataclass, field

from django.test import TestCase

from teatree.core.models import ScannedBroadcast
from teatree.loop.scanners.slack_broadcasts import MrState, SlackBroadcastsScanner
from teatree.types import RawAPIDict

MR_OPEN = "https://gitlab.example.com/team/project/-/merge_requests/9001"
CHANNELS = ["C0AAA", "C0BBB", "C0CCC"]
TS = "1779990001.000001"


@dataclass
class FakeMessaging:
    react_calls: list[tuple[str, str, str]] = field(default_factory=list)

    def react(self, *, channel: str, ts: str, emoji: str) -> RawAPIDict:
        self.react_calls.append((channel, ts, emoji))
        return {"ok": True}

    # Unused MessagingBackend surface — minimal stubs so the protocol is
    # satisfied at runtime without importing the bot backend.
    def fetch_mentions(self, *, since: str = "") -> list[RawAPIDict]:
        del since
        return []

    def fetch_dms(self, *, since: str = "") -> list[RawAPIDict]:
        del since
        return []

    def fetch_reactions(self, *, since: str = "") -> list[RawAPIDict]:
        del since
        return []

    def fetch_message(self, *, channel: str, ts: str) -> RawAPIDict:
        del channel, ts
        return {}

    def post_message(self, *, channel: str, text: str, thread_ts: str = "") -> RawAPIDict:
        del channel, text, thread_ts
        return {}

    def post_reply(self, *, channel: str, ts: str, text: str) -> RawAPIDict:
        del channel, ts, text
        return {}

    def open_dm(self, user_id: str) -> str:
        del user_id
        return ""

    def get_permalink(self, *, channel: str, ts: str) -> str:
        return f"https://slack.example/{channel}/p{ts.replace('.', '')}"

    def resolve_user_id(self, handle: str) -> str:
        del handle
        return ""

    def auth_test(self) -> RawAPIDict:
        return {"ok": True}


class MultiChannelBroadcastFanOutTests(TestCase):
    def test_same_mr_posted_to_three_channels_creates_three_rows_and_dispatches(self) -> None:
        # The same broadcast message appears in three independent channels
        # — capability A says the scanner fans out across all of them.
        messaging = FakeMessaging()
        messages_by_channel = {channel: [{"text": f"Please review {MR_OPEN}", "ts": TS}] for channel in CHANNELS}

        def fetch(*, channel: str) -> list[RawAPIDict]:
            return list(messages_by_channel.get(channel, []))

        def classifier(urls):
            return [MrState(url=url, merged=False, approved=False) for url in urls]

        scanner = SlackBroadcastsScanner(
            backend=messaging,
            channels=CHANNELS,
            fetch_channel_history=fetch,
            classify_mrs=classifier,
            overlay="test",
        )
        signals = scanner.scan()

        # One signal per (channel, open MR): three channels times one open MR.
        assert len(signals) == len(CHANNELS)
        # One ScannedBroadcast row per channel — the ledger doesn't
        # collapse the MR across channels.
        rows = list(ScannedBroadcast.objects.order_by("channel"))
        assert [row.channel for row in rows] == CHANNELS
        # No discovery-time :eyes: claim react on any channel (#113/#86) —
        # an open MR queues a dispatch but does not claim the review.
        assert messaging.react_calls == []
