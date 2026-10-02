"""Tests for the 👀-back self-ack — owner reacts to teatree's OWN message.

When the owner reacts (any emoji) to a message teatree itself authored, the
loop posts a ``:eyes:`` reaction back on that same message so the owner sees
the loop noticed. The self-ack rides inside
:class:`~teatree.loop.scanners.slack_review_intent.SlackReviewIntentScanner`
(the single owner of the ``slack-reactions.jsonl`` drain), consuming the same
drained reaction snapshot rather than racing a second drain (#1047).

The ack fires iff the reacting user is the owner AND the reacted message is
bot-authored, and is idempotent on ``(overlay, channel, item_ts)``.
"""

from dataclasses import dataclass, field

import pytest

from teatree.core.models import SlackSelfAckReaction
from teatree.loop.scanners.slack_review_intent import SlackReviewIntentScanner
from teatree.loop.scanners.slack_self_ack import SlackSelfAckReactor
from teatree.types import RawAPIDict

# ast-grep-ignore: ac-django-no-pytest-django-db
pytestmark = pytest.mark.django_db

OWNER = "U0OWNER"
BOT_USER = "U0BOT"
BOT_ID = "B0BOT"
OTHER = "U0OTHER"
CHANNEL = "D0OWNERDM"
TS = "1779180558.938799"


@dataclass
class FakeMessaging:
    """In-memory MessagingBackend for the self-ack tests.

    ``user_id`` is the OWNER (whose reactions we ack). ``auth.test`` resolves
    the BOT's own identity so bot-authorship of the reacted message can be
    proved. ``messages_by_ts`` maps ``(channel, ts)`` to the reacted message.
    """

    user_id: str = OWNER
    reactions: list[RawAPIDict] = field(default_factory=list)
    mentions: list[RawAPIDict] = field(default_factory=list)
    messages_by_ts: dict[tuple[str, str], RawAPIDict] = field(default_factory=dict)
    react_calls: list[tuple[str, str, str]] = field(default_factory=list)
    react_raises: bool = False
    auth_ok: bool = True

    def fetch_mentions(self, *, since: str = "") -> list[RawAPIDict]:
        _ = since
        events, self.mentions = self.mentions, []
        return events

    def fetch_dms(self, *, since: str = "") -> list[RawAPIDict]:
        _ = since
        return []

    def fetch_reactions(self, *, since: str = "") -> list[RawAPIDict]:
        _ = since
        events, self.reactions = self.reactions, []
        return events

    def fetch_message(self, *, channel: str, ts: str) -> RawAPIDict:
        return self.messages_by_ts.get((channel, ts), {})

    def react(self, *, channel: str, ts: str, emoji: str) -> RawAPIDict:
        if self.react_raises:
            msg = "slack 5xx"
            raise RuntimeError(msg)
        self.react_calls.append((channel, ts, emoji))
        return {"ok": True}

    def auth_test(self) -> RawAPIDict:
        if not self.auth_ok:
            return {"ok": False}
        return {"ok": True, "user_id": BOT_USER, "bot_id": BOT_ID}

    def resolve_user_id(self, handle: str) -> str:
        _ = handle
        return ""


def _reaction(*, user: str = OWNER, name: str = "thumbsup", channel: str = CHANNEL, ts: str = TS) -> RawAPIDict:
    return {
        "type": "reaction_added",
        "user": user,
        "reaction": name,
        "item": {"type": "message", "channel": channel, "ts": ts},
        "event_ts": ts,
    }


def _bot_message(channel: str = CHANNEL, ts: str = TS) -> RawAPIDict:
    return {"text": "here is your status", "ts": ts, "channel": channel, "user": BOT_USER, "bot_id": BOT_ID}


def _owner_message(channel: str = CHANNEL, ts: str = TS) -> RawAPIDict:
    return {"text": "a question from the owner", "ts": ts, "channel": channel, "user": OWNER}


class TestSelfAckReactor:
    def test_owner_reacts_to_bot_message_posts_eyes_once(self) -> None:
        backend = FakeMessaging(messages_by_ts={(CHANNEL, TS): _bot_message()})
        reactor = SlackSelfAckReactor(backend=backend, overlay="teatree")

        acked = reactor.ack_owner_reactions([_reaction()])

        assert acked == 1
        assert backend.react_calls == [(CHANNEL, TS, "eyes")]
        assert SlackSelfAckReaction.objects.filter(overlay="teatree", channel=CHANNEL, item_ts=TS).count() == 1

    def test_repeat_tick_does_not_re_react(self) -> None:
        backend = FakeMessaging(messages_by_ts={(CHANNEL, TS): _bot_message()})
        reactor = SlackSelfAckReactor(backend=backend, overlay="teatree")

        reactor.ack_owner_reactions([_reaction()])
        # A fresh reactor (new tick) re-observing the same event must not re-post.
        second = SlackSelfAckReactor(backend=backend, overlay="teatree").ack_owner_reactions([_reaction()])

        assert second == 0
        assert backend.react_calls == [(CHANNEL, TS, "eyes")]  # still exactly one

    def test_owner_reacts_to_non_bot_message_no_ack(self) -> None:
        backend = FakeMessaging(messages_by_ts={(CHANNEL, TS): _owner_message()})
        reactor = SlackSelfAckReactor(backend=backend, overlay="teatree")

        acked = reactor.ack_owner_reactions([_reaction()])

        assert acked == 0
        assert backend.react_calls == []
        assert SlackSelfAckReaction.objects.count() == 0

    def test_non_owner_reaction_no_ack(self) -> None:
        backend = FakeMessaging(messages_by_ts={(CHANNEL, TS): _bot_message()})
        reactor = SlackSelfAckReactor(backend=backend, overlay="teatree")

        acked = reactor.ack_owner_reactions([_reaction(user=OTHER)])

        assert acked == 0
        assert backend.react_calls == []
        assert SlackSelfAckReaction.objects.count() == 0

    def test_unresolved_bot_identity_fails_closed(self) -> None:
        # Without a resolvable bot identity we cannot prove bot-authorship, so
        # ack nothing rather than 👀 a message we did not write.
        backend = FakeMessaging(auth_ok=False, messages_by_ts={(CHANNEL, TS): _bot_message()})
        reactor = SlackSelfAckReactor(backend=backend, overlay="teatree")

        assert reactor.ack_owner_reactions([_reaction()]) == 0
        assert backend.react_calls == []
        assert SlackSelfAckReaction.objects.count() == 0

    def test_react_failure_releases_claim_for_retry(self) -> None:
        backend = FakeMessaging(react_raises=True, messages_by_ts={(CHANNEL, TS): _bot_message()})
        reactor = SlackSelfAckReactor(backend=backend, overlay="teatree")

        # The per-event guard swallows the raise; the claim is released so the
        # ack is not phantom-recorded and a later tick can retry.
        assert reactor.ack_owner_reactions([_reaction()]) == 0
        assert SlackSelfAckReaction.objects.count() == 0

    def test_missing_owner_id_no_ack(self) -> None:
        backend = FakeMessaging(user_id="", messages_by_ts={(CHANNEL, TS): _bot_message()})
        reactor = SlackSelfAckReactor(backend=backend, overlay="teatree")

        assert reactor.ack_owner_reactions([_reaction()]) == 0
        assert backend.react_calls == []


class TestSelfAckRidesInReviewIntentScanner:
    """The self-ack must fire from the review-intent scanner's reaction pass.

    The review-intent scanner is the single owner of the reactions drain, so
    the self-ack consuming that same snapshot is the only headless-correct
    wiring (a second drain would race the JSONL rename).
    """

    def test_owner_reaction_on_bot_message_acks_via_scanner(self) -> None:
        backend = FakeMessaging(
            reactions=[_reaction()],
            messages_by_ts={(CHANNEL, TS): _bot_message()},
        )

        signals = SlackReviewIntentScanner(backend=backend, overlay="teatree").scan()

        # No review URL on the message → no review-intent signal, but the
        # self-ack still 👀-backs the owner's reaction.
        assert signals == []
        assert backend.react_calls == [(CHANNEL, TS, "eyes")]
        assert SlackSelfAckReaction.objects.filter(channel=CHANNEL, item_ts=TS).count() == 1

    def test_non_owner_reaction_via_scanner_no_ack(self) -> None:
        backend = FakeMessaging(
            reactions=[_reaction(user=OTHER)],
            messages_by_ts={(CHANNEL, TS): _bot_message()},
        )

        SlackReviewIntentScanner(backend=backend, overlay="teatree").scan()

        assert backend.react_calls == []
        assert SlackSelfAckReaction.objects.count() == 0
