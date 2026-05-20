"""Tests for :class:`SlackBroadcastsScanner` — channel-poll broadcast loop (#1131).

The scanner polls one or more configured Slack channels, extracts MR URLs
from each message, classifies the set via an injected classifier, and:

* reacts ``:white_check_mark:`` and skips dispatch when every MR is merged +
    approved (the user mandate from #1131 comment 2);
* reacts ``:eyes:`` and emits one ``slack.review_intent`` signal per open
    MR in the open subset for mixed and all-pending broadcasts;
* persists one :class:`ScannedBroadcast` row per ``(channel, slack_ts)``
    for idempotent re-scans;
* hard-fails with :class:`ConnectChannelBotRestrictedError` on Slack-Connect
    bot-restricted channels until the dual-token write path (#1209) lands.
"""

from dataclasses import dataclass, field

import pytest
from django.test import TestCase

from teatree.core.models import ScannedBroadcast
from teatree.loop.scanners.slack_broadcasts import ConnectChannelBotRestrictedError, MrState, SlackBroadcastsScanner
from teatree.types import RawAPIDict

CHANNEL = "C0DEMOCHAN1"
TS_A = "1779201478.501469"
TS_B = "1779201499.123456"
MR_MERGED = "https://gitlab.example.com/team/project/-/merge_requests/6044"
MR_MERGED_2 = "https://gitlab.example.com/team/project/-/merge_requests/6224"
MR_OPEN = "https://gitlab.example.com/team/project/-/merge_requests/7432"
MR_OPEN_2 = "https://gitlab.example.com/team/project/-/merge_requests/7438"


@dataclass
class FakeMessaging:
    """Minimal MessagingBackend stub recording react calls."""

    user_id: str = "U0DEMOUSER1"
    react_calls: list[tuple[str, str, str]] = field(default_factory=list)
    react_raises: BaseException | None = None

    def react(self, *, channel: str, ts: str, emoji: str) -> RawAPIDict:
        if self.react_raises is not None:
            raise self.react_raises
        self.react_calls.append((channel, ts, emoji))
        return {"ok": True}

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

    def post_message(self, *, channel: str, text: str, thread_ts: str = "") -> RawAPIDict:
        _ = (channel, text, thread_ts)
        return {}

    def post_reply(self, *, channel: str, ts: str, text: str) -> RawAPIDict:
        _ = (channel, ts, text)
        return {}

    def open_dm(self, user_id: str) -> str:
        _ = user_id
        return ""

    def get_permalink(self, *, channel: str, ts: str) -> str:
        return f"https://slack.example/{channel}/p{ts.replace('.', '')}"

    def resolve_user_id(self, handle: str) -> str:
        _ = handle
        return ""

    def auth_test(self) -> RawAPIDict:
        return {"ok": True}


def _fetcher(messages_by_channel: dict[str, list[RawAPIDict]]):
    def fetch(*, channel: str) -> list[RawAPIDict]:
        return list(messages_by_channel.get(channel, []))

    return fetch


def _classifier(states: dict[str, MrState]):
    def classify(urls):
        return [states[url] for url in urls]

    return classify


def _message(text: str, ts: str) -> RawAPIDict:
    return {"text": text, "ts": ts, "user": "USRG", "type": "message"}


class TestClassificationBehaviour(TestCase):
    def test_all_merged_broadcast_reacts_green_check_and_skips_dispatch(self) -> None:
        backend = FakeMessaging()
        history = {CHANNEL: [_message(f"please review {MR_MERGED} and {MR_MERGED_2}", TS_A)]}
        states = {
            MR_MERGED: MrState(url=MR_MERGED, merged=True, approved=True),
            MR_MERGED_2: MrState(url=MR_MERGED_2, merged=True, approved=True),
        }
        scanner = SlackBroadcastsScanner(
            backend=backend,
            channels=[CHANNEL],
            fetch_channel_history=_fetcher(history),
            classify_mrs=_classifier(states),
        )

        signals = scanner.scan()

        assert signals == []
        assert backend.react_calls == [(CHANNEL, TS_A, "white_check_mark")]
        row = ScannedBroadcast.objects.get(channel=CHANNEL, slack_ts=TS_A)
        assert row.classification == ScannedBroadcast.Classification.ALL_MERGED

    def test_all_pending_broadcast_reacts_eyes_and_dispatches_every_url(self) -> None:
        backend = FakeMessaging()
        history = {CHANNEL: [_message(f"new review thread {MR_OPEN} {MR_OPEN_2}", TS_A)]}
        states = {
            MR_OPEN: MrState(url=MR_OPEN, merged=False, approved=False),
            MR_OPEN_2: MrState(url=MR_OPEN_2, merged=False, approved=False),
        }
        scanner = SlackBroadcastsScanner(
            backend=backend,
            channels=[CHANNEL],
            fetch_channel_history=_fetcher(history),
            classify_mrs=_classifier(states),
        )

        signals = scanner.scan()

        assert [s.kind for s in signals] == ["slack.review_intent", "slack.review_intent"]
        assert {s.payload["mr_url"] for s in signals} == {MR_OPEN, MR_OPEN_2}
        assert {s.payload["trigger"] for s in signals} == {"broadcast"}
        assert backend.react_calls == [(CHANNEL, TS_A, "eyes")]
        row = ScannedBroadcast.objects.get(channel=CHANNEL, slack_ts=TS_A)
        assert row.classification == ScannedBroadcast.Classification.PENDING
        assert row.mr_urls == [MR_OPEN, MR_OPEN_2]

    def test_mixed_broadcast_dispatches_only_open_subset(self) -> None:
        backend = FakeMessaging()
        history = {CHANNEL: [_message(f"{MR_MERGED} {MR_OPEN}", TS_A)]}
        states = {
            MR_MERGED: MrState(url=MR_MERGED, merged=True, approved=True),
            MR_OPEN: MrState(url=MR_OPEN, merged=False, approved=False),
        }
        scanner = SlackBroadcastsScanner(
            backend=backend,
            channels=[CHANNEL],
            fetch_channel_history=_fetcher(history),
            classify_mrs=_classifier(states),
        )

        signals = scanner.scan()

        assert len(signals) == 1
        assert signals[0].payload["mr_url"] == MR_OPEN
        assert backend.react_calls == [(CHANNEL, TS_A, "eyes")]


class TestIdempotency(TestCase):
    def test_idempotent_rescan_is_a_noop(self) -> None:
        backend = FakeMessaging()
        history = {CHANNEL: [_message(f"{MR_OPEN}", TS_A)]}
        states = {MR_OPEN: MrState(url=MR_OPEN, merged=False, approved=False)}
        scanner = SlackBroadcastsScanner(
            backend=backend,
            channels=[CHANNEL],
            fetch_channel_history=_fetcher(history),
            classify_mrs=_classifier(states),
        )

        first = scanner.scan()
        second = scanner.scan()

        assert len(first) == 1
        assert second == []
        # Only one react across the two scans — the second scan no-ops on the
        # idempotency row.
        assert backend.react_calls == [(CHANNEL, TS_A, "eyes")]
        assert ScannedBroadcast.objects.filter(channel=CHANNEL, slack_ts=TS_A).count() == 1

    def test_pending_to_all_merged_reclassifies_and_reacts_green(self) -> None:
        backend = FakeMessaging()
        history = {CHANNEL: [_message(f"{MR_OPEN}", TS_A)]}
        pending_states = {MR_OPEN: MrState(url=MR_OPEN, merged=False, approved=False)}
        merged_states = {MR_OPEN: MrState(url=MR_OPEN, merged=True, approved=True)}

        pending_scanner = SlackBroadcastsScanner(
            backend=backend,
            channels=[CHANNEL],
            fetch_channel_history=_fetcher(history),
            classify_mrs=_classifier(pending_states),
        )
        pending_scanner.scan()

        merged_scanner = SlackBroadcastsScanner(
            backend=backend,
            channels=[CHANNEL],
            fetch_channel_history=_fetcher(history),
            classify_mrs=_classifier(merged_states),
        )
        signals = merged_scanner.scan()

        assert signals == []
        assert backend.react_calls == [
            (CHANNEL, TS_A, "eyes"),
            (CHANNEL, TS_A, "white_check_mark"),
        ]
        row = ScannedBroadcast.objects.get(channel=CHANNEL, slack_ts=TS_A)
        assert row.classification == ScannedBroadcast.Classification.ALL_MERGED
        assert row.reclassified_at is not None


class TestConnectChannelHardFail(TestCase):
    def test_connect_channel_bot_restricted_hard_fails(self) -> None:
        backend = FakeMessaging(react_raises=RuntimeError("Slack API not_in_channel"))
        history = {CHANNEL: [_message(f"{MR_OPEN}", TS_A)]}
        states = {MR_OPEN: MrState(url=MR_OPEN, merged=False, approved=False)}
        scanner = SlackBroadcastsScanner(
            backend=backend,
            channels=[CHANNEL],
            fetch_channel_history=_fetcher(history),
            classify_mrs=_classifier(states),
        )

        with pytest.raises(ConnectChannelBotRestrictedError) as exc_info:
            scanner.scan()

        assert exc_info.value.channel == CHANNEL


class TestNoiseHandling(TestCase):
    def test_messages_without_mr_urls_are_ignored(self) -> None:
        backend = FakeMessaging()
        history = {
            CHANNEL: [
                _message("good morning team", TS_A),
                _message(f"and another {MR_OPEN}", TS_B),
            ],
        }
        states = {MR_OPEN: MrState(url=MR_OPEN, merged=False, approved=False)}
        scanner = SlackBroadcastsScanner(
            backend=backend,
            channels=[CHANNEL],
            fetch_channel_history=_fetcher(history),
            classify_mrs=_classifier(states),
        )

        signals = scanner.scan()

        assert len(signals) == 1
        assert signals[0].payload["mr_url"] == MR_OPEN
        assert backend.react_calls == [(CHANNEL, TS_B, "eyes")]
        assert ScannedBroadcast.objects.count() == 1
