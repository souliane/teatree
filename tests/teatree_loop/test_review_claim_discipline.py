"""Review-claim discipline — the single chokepoint that fixes #113 / #86 / #88 / #123.

The binding rules, each pinned by a symmetric must-fire / must-NOT-fire pair:

1. A review claim (``:eyes:`` reaction, ``slack.review_intent`` dispatch) is
    emitted ONLY at review-DONE, never at discovery/claim time.
2. While the review loop is stopped, zero review-intent signals are emitted
    and zero reviewer dispatches are queued.
3. A reaction already present (colleague or bot, on the message or in the
    ledger) is never re-added.
4. No per-tick re-fire: a reaction the loop posts is recorded so a second
    tick skips it.
5. Review-DONE posts ``:eyes:`` + the verdict emoji (``:white_check_mark:``
    clean / ``:question:`` blocking) — the ONLY Slack signal, never an
    author DM.
"""

import os
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field

import pytest
from django.test import TestCase

from teatree.core.models import OutboundClaim, ReviewRequestPost, ReviewVerdict
from teatree.loop.dispatch import dispatch
from teatree.loop.review_claim import (
    emit_review_done_reactions,
    filter_review_intent_signals,
    reaction_already_present,
    record_reaction_claim,
    review_loop_enabled,
)
from teatree.loop.scanners.base import ScanSignal
from teatree.loop.scanners.slack_broadcasts import MrState, SlackBroadcastsScanner
from teatree.types import RawAPIDict
from tests.teatree_core._on_behalf_gate_helpers import disable_on_behalf_gate

pytestmark = pytest.mark.django_db


@pytest.fixture(autouse=True)
def _gate_off(tmp_path_factory: pytest.TempPathFactory, monkeypatch: pytest.MonkeyPatch) -> None:
    disable_on_behalf_gate(tmp_path_factory, monkeypatch)


CHANNEL = "C0REVIEW"
TS = "1779990001.000001"
MR = "https://gitlab.example.com/team/project/-/merge_requests/7567"
COLLEAGUE = "UCOLLEAGUE"
USER = "U0A72P7CK0A"
_SHA = "c" * 40


@dataclass
class _FakeMessaging:
    """Records react/react_routed calls and serves canned reaction-get reads."""

    user_id: str = USER
    react_calls: list[tuple[str, str, str]] = field(default_factory=list)
    react_routed_calls: list[tuple[str, str, str]] = field(default_factory=list)
    routed_response: RawAPIDict = field(default_factory=lambda: {"ok": True})
    dm_calls: list[tuple[str, str]] = field(default_factory=list)

    def react(self, *, channel: str, ts: str, emoji: str) -> RawAPIDict:
        self.react_calls.append((channel, ts, emoji))
        return {"ok": True}

    def react_routed(self, *, channel: str, ts: str, emoji: str) -> RawAPIDict:
        self.react_routed_calls.append((channel, ts, emoji))
        return dict(self.routed_response)

    def open_dm(self, user_id: str) -> str:
        self.dm_calls.append(("open_dm", user_id))
        return "D0FAKE"

    def post_message(self, *, channel: str, text: str, thread_ts: str = "") -> RawAPIDict:
        self.dm_calls.append((channel, text))
        return {"ok": True}


@contextmanager
def _review_loop_disabled() -> Iterator[None]:
    """Disable the review mini-loop via the env kill-switch for the block."""
    previous = os.environ.get("T3_LOOPS_DISABLED")
    os.environ["T3_LOOPS_DISABLED"] = "review"
    try:
        yield
    finally:
        if previous is None:
            del os.environ["T3_LOOPS_DISABLED"]
        else:
            os.environ["T3_LOOPS_DISABLED"] = previous


def _fetcher(messages_by_channel: dict[str, list[RawAPIDict]]):
    def fetch(*, channel: str) -> list[RawAPIDict]:
        return list(messages_by_channel.get(channel, []))

    return fetch


def _classifier(states: dict[str, MrState]):
    def classify(urls):
        return [states[url] for url in urls]

    return classify


def _broadcast_scanner(backend: _FakeMessaging) -> SlackBroadcastsScanner:
    return SlackBroadcastsScanner(
        backend=backend,
        channels=[CHANNEL],
        fetch_channel_history=_fetcher({CHANNEL: [{"text": f"please review {MR}", "ts": TS, "type": "message"}]}),
        classify_mrs=_classifier({MR: MrState(url=MR, merged=False, approved=False, author_username="colleague")}),
    )


def _review_intent_signal() -> ScanSignal:
    return ScanSignal(
        kind="slack.review_intent",
        summary=f"Review intent (broadcast): {MR}",
        payload={"url": MR, "mr_url": MR, "channel": CHANNEL, "ts": TS, "trigger": "broadcast"},
    )


class TestNoClaimAtDiscovery(TestCase):
    """Rule 1: discovery emits the dispatch signal but NEVER a claim reaction."""

    def test_open_colleague_mr_dispatches_without_eyes_reaction(self) -> None:
        backend = _FakeMessaging()
        signals = _broadcast_scanner(backend).scan()

        assert [s.kind for s in signals] == ["slack.review_intent"]
        # The claim reaction must NOT fire at discovery.
        assert backend.react_calls == []
        assert backend.react_routed_calls == []

    def test_discovery_records_no_reaction_claim_in_the_ledger(self) -> None:
        backend = _FakeMessaging()
        _broadcast_scanner(backend).scan()

        assert OutboundClaim.objects.filter(kind=OutboundClaim.Kind.SLACK_REACTION).count() == 0


class TestReviewDoneReactions(TestCase):
    """Rule 1 + 5: the verdict emoji set fires at review-DONE, deduped, no DM."""

    def _post(self) -> ReviewRequestPost:
        return ReviewRequestPost.objects.create(mr_url=MR, slack_channel_id=CHANNEL, slack_thread_ts=TS)

    def test_merge_safe_done_posts_eyes_plus_white_check_mark(self) -> None:
        self._post()
        backend = _FakeMessaging()
        verdict = ReviewVerdict.record(
            pr_id=7567,
            slug="team/project",
            reviewed_sha=_SHA,
            verdict="merge_safe",
            reviewer_identity="cold-reviewer",
        )

        posted = emit_review_done_reactions(
            slug="team/project", pr_id=7567, emojis=verdict.done_reaction_emojis(), messaging=backend
        )

        assert posted == ["eyes", "white_check_mark"]
        assert backend.react_routed_calls == [(CHANNEL, TS, "eyes"), (CHANNEL, TS, "white_check_mark")]
        # The Slack reaction is the ONLY signal — never an author DM.
        assert backend.dm_calls == []

    def test_hold_done_posts_eyes_plus_question(self) -> None:
        self._post()
        backend = _FakeMessaging()
        verdict = ReviewVerdict.record(
            pr_id=7567,
            slug="team/project",
            reviewed_sha=_SHA,
            verdict="hold",
            reviewer_identity="cold-reviewer",
            gh_verify_result="green",
        )

        posted = emit_review_done_reactions(
            slug="team/project", pr_id=7567, emojis=verdict.done_reaction_emojis(), messaging=backend
        )

        assert posted == ["eyes", "question"]
        assert backend.react_routed_calls == [(CHANNEL, TS, "eyes"), (CHANNEL, TS, "question")]
        assert backend.dm_calls == []

    def test_done_with_no_tracked_message_posts_nothing(self) -> None:
        backend = _FakeMessaging()
        posted = emit_review_done_reactions(
            slug="team/project", pr_id=7567, emojis=("eyes", "white_check_mark"), messaging=backend
        )
        assert posted == []
        assert backend.react_routed_calls == []


class TestReactionDedup(TestCase):
    """Rule 3 + 4: never re-add a reaction already present or already recorded."""

    def _post(self) -> None:
        ReviewRequestPost.objects.create(mr_url=MR, slack_channel_id=CHANNEL, slack_thread_ts=TS)

    def test_ledger_recorded_reaction_is_not_re_added(self) -> None:
        self._post()
        record_reaction_claim(channel=CHANNEL, ts=TS, emoji="white_check_mark", target_url=MR)
        backend = _FakeMessaging()

        posted = emit_review_done_reactions(
            slug="team/project", pr_id=7567, emojis=("eyes", "white_check_mark"), messaging=backend
        )

        # ``eyes`` is fresh; ``white_check_mark`` is already in the ledger.
        assert posted == ["eyes"]
        assert backend.react_routed_calls == [(CHANNEL, TS, "eyes")]

    def test_second_emission_does_not_re_fire(self) -> None:
        self._post()
        backend = _FakeMessaging()
        first = emit_review_done_reactions(
            slug="team/project", pr_id=7567, emojis=("eyes", "white_check_mark"), messaging=backend
        )
        second = emit_review_done_reactions(
            slug="team/project", pr_id=7567, emojis=("eyes", "white_check_mark"), messaging=backend
        )

        assert first == ["eyes", "white_check_mark"]
        assert second == []
        # Only the first tick reacted — the second is fully deduped.
        assert backend.react_routed_calls == [(CHANNEL, TS, "eyes"), (CHANNEL, TS, "white_check_mark")]

    def test_reaction_present_from_colleague_on_message_is_skipped(self) -> None:
        message = {
            "reactions": [{"name": "white_check_mark", "users": [COLLEAGUE], "count": 1}],
        }
        assert reaction_already_present(message=message, channel=CHANNEL, ts=TS, emoji="white_check_mark") is True
        assert reaction_already_present(message=message, channel=CHANNEL, ts=TS, emoji="eyes") is False


class TestReviewLoopStopped(TestCase):
    """Rule 2: a stopped review loop emits zero review-intent signals/dispatches."""

    def test_signals_pass_through_when_review_loop_enabled(self) -> None:
        signals = [_review_intent_signal()]
        assert filter_review_intent_signals(signals) == signals

    def test_signals_dropped_when_review_loop_disabled(self) -> None:
        with _review_loop_disabled():
            assert review_loop_enabled() is False
            assert filter_review_intent_signals([_review_intent_signal()]) == []

    def test_broadcast_scanner_emits_nothing_when_review_loop_disabled(self) -> None:
        backend = _FakeMessaging()
        with _review_loop_disabled():
            signals = _broadcast_scanner(backend).scan()

        assert signals == []
        assert backend.react_calls == []

    def test_dispatch_drops_review_intent_when_review_loop_disabled(self) -> None:
        with _review_loop_disabled():
            actions = dispatch([_review_intent_signal()])

        assert actions == []

    def test_dispatch_routes_review_intent_to_reviewer_when_enabled(self) -> None:
        actions = dispatch([_review_intent_signal()])
        assert any(a.kind == "agent" and a.zone == "t3:reviewer" for a in actions)


class TestReviewRecordCommandEmitsReaction(TestCase):
    """``t3 review record`` posts the verdict reaction set, never an author DM."""

    def test_record_merge_safe_posts_eyes_and_check_no_dm(self) -> None:
        from unittest.mock import patch  # noqa: PLC0415

        from django.core.management import call_command  # noqa: PLC0415

        ReviewRequestPost.objects.create(mr_url=MR, slack_channel_id=CHANNEL, slack_thread_ts=TS)
        backend = _FakeMessaging()

        with patch(
            "teatree.core.backend_factory.messaging_from_overlay",
            return_value=backend,
        ):
            call_command(
                "review",
                "record",
                "7567",
                "team/project",
                "--reviewed-sha",
                _SHA,
                "--verdict",
                "merge_safe",
                "--reviewer-identity",
                "cold-reviewer",
            )

        assert backend.react_routed_calls == [(CHANNEL, TS, "eyes"), (CHANNEL, TS, "white_check_mark")]
        assert backend.dm_calls == []
