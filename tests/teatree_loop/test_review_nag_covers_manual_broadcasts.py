"""ReviewNagScanner must nag both bot-tracked AND manually-broadcast MRs (#1256).

The ``ReviewNagScanner`` walks ``ReviewRequestPost`` rows on every tick.
Before #1256 the only writers of those rows were the bot's review-request
flow (``record_review_request_post``); a manual post in the review channel
(detected by ``SlackBroadcastsScanner``) created a ``ScannedBroadcast`` row
but no ``ReviewRequestPost`` row, leaving the nag scanner blind to it.

The fix wires ``SlackBroadcastsScanner`` to also seed a ``ReviewRequestPost``
for every open MR it ingests, so both paths feed the same nag pipeline.
"""

import datetime as dt
from dataclasses import dataclass, field
from typing import Any
from unittest.mock import patch

import pytest
from django.test import TestCase
from django.utils import timezone

from teatree.config import OnBehalfPostMode, TeaTreeConfig, UserSettings
from teatree.core.models import ReviewRequestPost
from teatree.loop.review_request_tracker import record_review_request_post
from teatree.loop.scanners.review_nag import ReviewNagScanner
from teatree.loop.scanners.slack_broadcasts import MrState, SlackBroadcastsScanner
from teatree.types import RawAPIDict
from tests.teatree_core._on_behalf_gate_helpers import disable_on_behalf_gate


@pytest.fixture(autouse=True)
def _gate_off(tmp_path_factory: pytest.TempPathFactory, monkeypatch: pytest.MonkeyPatch) -> None:
    disable_on_behalf_gate(tmp_path_factory, monkeypatch)


CHANNEL = "C0DEMOCHAN1"
BOT_THREAD_TS = "1700000000.001"
MANUAL_TS = "1700000099.999"
MR_BOT = "https://gitlab.example.com/team/project/-/merge_requests/7400"
MR_MANUAL = "https://gitlab.example.com/team/project/-/merge_requests/7437"


@dataclass
class FakeSlack:
    """In-memory messaging backend recording posts and reacts."""

    posts: list[dict[str, Any]] = field(default_factory=list)
    react_calls: list[tuple[str, str, str]] = field(default_factory=list)
    usergroup_id: str = ""
    dm_channel: str = "D-USER"

    def fetch_mentions(self, *, since: str = "") -> list[RawAPIDict]:
        _ = since
        return []

    def fetch_dms(self, *, since: str = "") -> list[RawAPIDict]:
        _ = since
        return []

    def fetch_reactions(self, *, since: str = "") -> list[RawAPIDict]:
        _ = since
        return []

    def fetch_message(self, *, channel: str, ts: str) -> RawAPIDict:
        _ = (channel, ts)
        return {}

    def fetch_thread_replies(self, *, channel: str, thread_ts: str) -> list[RawAPIDict]:
        _ = (channel, thread_ts)
        return []

    def post_message(self, *, channel: str, text: str, thread_ts: str = "") -> RawAPIDict:
        self.posts.append({"channel": channel, "text": text, "thread_ts": thread_ts})
        return {"ok": True, "ts": f"reply.{len(self.posts)}"}

    def post_routed(self, *, channel: str, text: str, thread_ts: str = "") -> RawAPIDict:
        return self.post_message(channel=channel, text=text, thread_ts=thread_ts)

    def post_reply(self, *, channel: str, ts: str, text: str) -> RawAPIDict:
        return self.post_message(channel=channel, text=text, thread_ts=ts)

    def open_dm(self, user_id: str) -> str:
        _ = user_id
        return self.dm_channel

    def get_permalink(self, *, channel: str, ts: str) -> str:
        return f"https://slack.example/{channel}/p{ts.replace('.', '')}"

    def react(self, *, channel: str, ts: str, emoji: str) -> RawAPIDict:
        self.react_calls.append((channel, ts, emoji))
        return {"ok": True}

    def react_routed(self, *, channel: str, ts: str, emoji: str) -> RawAPIDict:
        self.react_calls.append((channel, ts, emoji))
        return {"ok": True}

    def resolve_user_id(self, handle: str) -> str:
        if handle == "engineers":
            return self.usergroup_id
        return ""

    def auth_test(self) -> RawAPIDict:
        return {"ok": True}


def _fetcher(messages: dict[str, list[RawAPIDict]]):
    def fetch(*, channel: str) -> list[RawAPIDict]:
        return list(messages.get(channel, []))

    return fetch


def _classifier(states: dict[str, MrState]):
    def classify(urls):
        return [states[url] for url in urls]

    return classify


class TestReviewNagCoversBothPaths(TestCase):
    """Both bot-tracked AND manually-broadcast rows must get nagged."""

    def setUp(self) -> None:
        super().setUp()
        enabled = TeaTreeConfig(
            user=UserSettings(review_nag_enabled=True, on_behalf_post_mode=OnBehalfPostMode.IMMEDIATE),
        )
        patcher = patch("teatree.config.load_config", return_value=enabled)
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_nag_fires_on_bot_tracked_and_manually_broadcast_mr(self) -> None:
        # --- 1. Bot-created row (the existing path) ---
        record_review_request_post(
            mr_url=MR_BOT,
            slack_channel_id=CHANNEL,
            slack_thread_ts=BOT_THREAD_TS,
        )

        # --- 2. Manually-posted row (the gap #1256 closes) ---
        # A colleague (or the user) hand-posts an MR in the review channel,
        # outside the teatree review-request flow. The broadcast scanner
        # ingests it on the next tick — and must seed a ReviewRequestPost
        # row so the nag scanner can find it.
        backend = FakeSlack()
        history = {
            CHANNEL: [{"text": f"please review {MR_MANUAL}", "ts": MANUAL_TS, "user": "USRG", "type": "message"}]
        }
        states = {MR_MANUAL: MrState(url=MR_MANUAL, merged=False, approved=False)}
        SlackBroadcastsScanner(
            backend=backend,
            channels=[CHANNEL],
            fetch_channel_history=_fetcher(history),
            classify_mrs=_classifier(states),
        ).scan()

        # The broadcast scanner must have seeded a ReviewRequestPost for
        # the manually-posted MR.
        assert ReviewRequestPost.objects.filter(mr_url=MR_MANUAL).exists(), (
            "SlackBroadcastsScanner did not seed a ReviewRequestPost for the "
            "manually-broadcast MR — the nag scanner is blind to it (#1256)."
        )

        # --- 3. Both rows are now idle for > 2 days ---
        old = timezone.now() - dt.timedelta(days=3)
        ReviewRequestPost.objects.filter(mr_url__in=[MR_BOT, MR_MANUAL]).update(created_at=old)

        # --- 4. The nag scanner must fire on BOTH threads ---
        nag_slack = FakeSlack()
        signals = ReviewNagScanner(messaging=nag_slack).scan()

        pinged_threads = {p["thread_ts"] for p in nag_slack.posts}
        assert BOT_THREAD_TS in pinged_threads, "ReviewNagScanner did not nag the bot-tracked MR thread"
        assert MANUAL_TS in pinged_threads, "ReviewNagScanner did not nag the manually-broadcast MR thread (#1256)"
        assert all(p["text"].endswith(":pray:") for p in nag_slack.posts)
        kinds = [s.kind for s in signals]
        assert kinds.count("review_nag.ping") == 2, f"expected two pings, got {kinds}"
