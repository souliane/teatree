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

from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field

import pytest
from django.test import TestCase

from teatree.core.models import LoopState, OutboundClaim, ReviewRequestPost, ReviewVerdict
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

# ast-grep-ignore: ac-django-no-pytest-django-db
pytestmark = pytest.mark.django_db


@pytest.fixture(autouse=True)
def _gate_off(tmp_path_factory: pytest.TempPathFactory, monkeypatch: pytest.MonkeyPatch) -> None:
    disable_on_behalf_gate(tmp_path_factory, monkeypatch)


CHANNEL = "C0REVIEW"
TS = "1779990001.000001"
MR = "https://gitlab.example.com/team/project/-/merge_requests/7567"
COLLEAGUE = "UCOLLEAGUE"
USER = "U0DEMOUSER1"
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
    """Disable the review mini-loop via the durable DB ``LoopState`` for the block.

    Loop control is DB-only — ``t3 loop disable review`` (a ``DISABLED``
    ``LoopState`` row) is what stops review claims; there is no env kill-switch.
    """
    LoopState.objects.disable("review")
    try:
        yield
    finally:
        LoopState.objects.resume("review")


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


class TestEgressReactPayload(TestCase):
    """Pin the EXACT colleague-egress payload ``_egress_react`` builds (#2413 PR-2).

    ``emit_review_done_reactions`` asserts only the posted emoji list; the
    ``action`` / ``destination`` / ``artifact_url`` / ``summary`` / ``target``
    strings that drive the on-behalf gate's audit key and the after-receipt DM
    are unasserted, so their mutants survive. These assertions kill them.
    """

    def _post(self) -> None:
        ReviewRequestPost.objects.create(mr_url=MR, slack_channel_id=CHANNEL, slack_thread_ts=TS)

    @staticmethod
    def _captured_react_kwargs(captured: list[dict[str, str]]):
        from teatree.core.on_behalf_egress import OnBehalfSlackEgress  # noqa: PLC0415

        class _SpyEgress:
            def __init__(self, messaging: object) -> None:
                self._inner = OnBehalfSlackEgress(messaging)

            def react(self, **kwargs: str) -> RawAPIDict:
                captured.append(dict(kwargs))
                return self._inner.react(**kwargs)

        return _SpyEgress

    def test_react_payload_carries_emoji_scoped_action_destination_summary(self) -> None:
        from unittest.mock import patch  # noqa: PLC0415

        self._post()
        backend = _FakeMessaging()
        captured: list[dict[str, str]] = []

        with patch("teatree.loop.review_claim.OnBehalfSlackEgress", self._captured_react_kwargs(captured)):
            posted = emit_review_done_reactions(slug="team/project", pr_id=7567, emojis=("eyes",), messaging=backend)

        assert posted == ["eyes"]
        assert captured == [
            {
                "channel": CHANNEL,
                "ts": TS,
                "emoji": "eyes",
                "target": MR,
                "action": "review_done_reaction:eyes",
                "destination": f"review-request for {MR}",
                "artifact_url": MR,
                "summary": ":eyes: review-DONE reaction",
            }
        ]

    def test_already_reacted_response_counts_as_posted(self) -> None:
        self._post()
        backend = _FakeMessaging(routed_response={"ok": False, "error": "already_reacted"})

        posted = emit_review_done_reactions(slug="team/project", pr_id=7567, emojis=("eyes",), messaging=backend)

        # ``already_reacted`` is success — the emoji IS present — so it is recorded.
        assert posted == ["eyes"]
        assert OutboundClaim.objects.filter(kind=OutboundClaim.Kind.SLACK_REACTION).count() == 1

    def test_not_ok_without_already_reacted_is_not_posted(self) -> None:
        self._post()
        backend = _FakeMessaging(routed_response={"ok": False, "error": "channel_not_found"})

        posted = emit_review_done_reactions(slug="team/project", pr_id=7567, emojis=("eyes",), messaging=backend)

        # A genuine transport failure is NOT success — nothing posted, nothing recorded.
        assert posted == []
        assert OutboundClaim.objects.filter(kind=OutboundClaim.Kind.SLACK_REACTION).count() == 0

    def test_non_dict_response_is_not_posted(self) -> None:
        self._post()

        @dataclass
        class _NonDictReactBackend(_FakeMessaging):
            def react_routed(self, *, channel: str, ts: str, emoji: str) -> RawAPIDict:
                self.react_routed_calls.append((channel, ts, emoji))
                return ["not", "a", "dict"]  # type: ignore[return-value]

        backend = _NonDictReactBackend()
        posted = emit_review_done_reactions(slug="team/project", pr_id=7567, emojis=("eyes",), messaging=backend)

        assert posted == []


class TestSlackMessageResolution(TestCase):
    """Pin ``_slack_message_for_pr`` row selection (#2413 PR-2 mutation paydown).

    The resolver matches the FIRST ``ReviewRequestPost`` whose URL parses to the
    same ``(slug, pr_id)``, requires a non-empty ``slack_thread_ts`` and
    ``slack_channel_id``, and returns ``(channel, ts, mr_url)`` in that order.
    Each clause below kills the corresponding mutant.
    """

    OTHER_MR = "https://gitlab.example.com/team/project/-/merge_requests/9999"
    OTHER_SLUG_MR = "https://gitlab.example.com/other/repo/-/merge_requests/7567"

    def test_resolves_channel_ts_url_in_order_for_matching_slug_and_pr_id(self) -> None:
        ReviewRequestPost.objects.create(mr_url=MR, slack_channel_id=CHANNEL, slack_thread_ts=TS)

        backend = _FakeMessaging()
        posted = emit_review_done_reactions(slug="team/project", pr_id=7567, emojis=("eyes",), messaging=backend)

        assert posted == ["eyes"]
        # Tuple order (channel, ts, url) drives the react coordinates: a swapped
        # tuple would react on the wrong channel/ts.
        assert backend.react_routed_calls == [(CHANNEL, TS, "eyes")]

    def test_wrong_pr_id_row_is_not_matched(self) -> None:
        ReviewRequestPost.objects.create(mr_url=self.OTHER_MR, slack_channel_id="C0WRONG", slack_thread_ts="9.9")

        backend = _FakeMessaging()
        posted = emit_review_done_reactions(slug="team/project", pr_id=7567, emojis=("eyes",), messaging=backend)

        # pr_id 7567 != 9999 — no row matches, nothing posted.
        assert posted == []
        assert backend.react_routed_calls == []

    def test_wrong_slug_row_is_not_matched(self) -> None:
        ReviewRequestPost.objects.create(mr_url=self.OTHER_SLUG_MR, slack_channel_id="C0WRONG", slack_thread_ts="9.9")

        backend = _FakeMessaging()
        posted = emit_review_done_reactions(slug="team/project", pr_id=7567, emojis=("eyes",), messaging=backend)

        # slug other/repo != team/project — no match.
        assert posted == []
        assert backend.react_routed_calls == []

    def test_row_with_empty_thread_ts_is_excluded(self) -> None:
        ReviewRequestPost.objects.create(mr_url=MR, slack_channel_id=CHANNEL, slack_thread_ts="")

        backend = _FakeMessaging()
        posted = emit_review_done_reactions(slug="team/project", pr_id=7567, emojis=("eyes",), messaging=backend)

        # An untracked thread (empty ts) has no Slack message to react on.
        assert posted == []
        assert backend.react_routed_calls == []

    def test_row_with_empty_channel_is_not_matched(self) -> None:
        ReviewRequestPost.objects.create(mr_url=MR, slack_channel_id="", slack_thread_ts=TS)

        backend = _FakeMessaging()
        posted = emit_review_done_reactions(slug="team/project", pr_id=7567, emojis=("eyes",), messaging=backend)

        # No channel id — the post can't be located, so nothing reacts.
        assert posted == []
        assert backend.react_routed_calls == []

    def test_empty_slug_or_zero_pr_id_short_circuits(self) -> None:
        ReviewRequestPost.objects.create(mr_url=MR, slack_channel_id=CHANNEL, slack_thread_ts=TS)
        backend = _FakeMessaging()

        assert emit_review_done_reactions(slug="", pr_id=7567, emojis=("eyes",), messaging=backend) == []
        assert emit_review_done_reactions(slug="team/project", pr_id=0, emojis=("eyes",), messaging=backend) == []
        assert backend.react_routed_calls == []


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
