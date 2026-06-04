"""Post-merge ``:white_check_mark:`` sweep tests (#1295 capability C).

When a broadcast on channel A flips to ``all_merged``, the scanner
replicates the ``:white_check_mark:`` reaction onto every sibling
broadcast (channel B, C, …) that carries the same MR URL, skipping the
channel that already has the green check.
"""

from dataclasses import dataclass, field

import pytest
from django.test import TestCase

from teatree.core.models import BroadcastObservation, ScannedBroadcast
from teatree.loop.scanners.slack_broadcasts import MrState, SlackBroadcastsScanner
from teatree.types import RawAPIDict
from tests.teatree_core._on_behalf_gate_helpers import disable_on_behalf_gate

MR = "https://gitlab.example.com/team/proj/-/merge_requests/777"
CHANNELS = ["C_AAA", "C_BBB", "C_CCC"]


@pytest.fixture(autouse=True)
def _gate_off(tmp_path_factory: pytest.TempPathFactory, monkeypatch: pytest.MonkeyPatch) -> None:
    disable_on_behalf_gate(tmp_path_factory, monkeypatch)


@dataclass
class FakeMessaging:
    react_calls: list[tuple[str, str, str]] = field(default_factory=list)

    def react(self, *, channel: str, ts: str, emoji: str) -> RawAPIDict:
        self.react_calls.append((channel, ts, emoji))
        return {"ok": True}

    def react_routed(self, *, channel: str, ts: str, emoji: str) -> RawAPIDict:
        return self.react(channel=channel, ts=ts, emoji=emoji)

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


class WhiteCheckMarkSweepTests(TestCase):
    def test_post_merge_sweep_replicates_check_to_sibling_broadcasts(self) -> None:
        # Seed: channels B and C already have ALL_MERGED rows for the
        # same MR (broadcast that was previously scanned and flipped).
        ts = "1779990010.000001"
        for channel in CHANNELS[1:]:
            ScannedBroadcast.record(
                BroadcastObservation(
                    channel=channel,
                    slack_ts=ts,
                    mr_urls=[MR],
                    classification=ScannedBroadcast.Classification.ALL_MERGED.value,
                    overlay="test",
                ),
            )

        # Now scan channel A — its broadcast is the freshly-merged one.
        messaging = FakeMessaging()
        messages = {CHANNELS[0]: [{"text": f"Merge complete {MR}", "ts": ts}]}

        def fetch(*, channel: str) -> list[RawAPIDict]:
            return list(messages.get(channel, []))

        def classifier(urls):
            return [MrState(url=url, merged=True, approved=True) for url in urls]

        scanner = SlackBroadcastsScanner(
            backend=messaging,
            channels=[CHANNELS[0]],
            fetch_channel_history=fetch,
            classify_mrs=classifier,
            overlay="test",
        )
        scanner.scan()

        # White-check-mark reaction on A (the just-merged) plus B and C
        # (the sibling broadcasts).
        emojis_by_channel = {(c, e) for c, _ts, e in messaging.react_calls}
        for channel in CHANNELS:
            assert (channel, "white_check_mark") in emojis_by_channel, messaging.react_calls
