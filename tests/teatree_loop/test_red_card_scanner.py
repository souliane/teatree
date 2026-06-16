"""Tests for :class:`RedCardScanner` — user RED CARD signal detection (#1130).

The scanner watches three surfaces:

1. ``:red_circle:`` reactions added by the user to an agent message.
2. ``:no_entry_sign:`` reactions added by the user to an agent message.
3. The literal phrase ``"RED CARD"`` (case-insensitive, optional dash or
    space between the words) in a user DM or thread reply.

Each fresh trigger creates one :class:`RedCardSignal` row (idempotent on
``(overlay, channel, slack_ts)``), posts the ``:eyes:`` acknowledgement on
the user's signal, and emits one ``red_card.signal`` ``ScanSignal`` so the
dispatcher can route it to the coordinator's corrective-action workflow.
"""

from dataclasses import dataclass, field

import pytest

from teatree.core.models import RedCardSignal
from teatree.loop.scanners.red_card import RedCardScanner
from teatree.types import RawAPIDict

# ast-grep-ignore: ac-django-no-pytest-django-db
pytestmark = pytest.mark.django_db


USER = "U0DEMOUSER1"
OTHER_USER = "UOTHER"
CHANNEL = "C0REVIEWCHAN"
DM_CHANNEL = "D0DEMOTEAM1"
AGENT_TS = "1779180557.000100"
SIGNAL_TS = "1779180558.938799"


@dataclass
class FakeMessaging:
    """In-memory MessagingBackend for RED CARD scanner tests."""

    user_id: str = USER
    reactions: list[RawAPIDict] = field(default_factory=list)
    dms: list[RawAPIDict] = field(default_factory=list)
    messages_by_ts: dict[tuple[str, str], RawAPIDict] = field(default_factory=dict)
    react_calls: list[tuple[str, str, str]] = field(default_factory=list)

    def fetch_mentions(self, *, since: str = "") -> list[RawAPIDict]:
        _ = since
        return []

    def fetch_dms(self, *, since: str = "") -> list[RawAPIDict]:
        _ = since
        events, self.dms = self.dms, []
        return events

    def fetch_reactions(self, *, since: str = "") -> list[RawAPIDict]:
        _ = since
        events, self.reactions = self.reactions, []
        return events

    def fetch_message(self, *, channel: str, ts: str) -> RawAPIDict:
        return self.messages_by_ts.get((channel, ts), {})

    def post_message(self, *, channel: str, text: str, thread_ts: str = "") -> RawAPIDict:
        _ = channel, text, thread_ts
        return {}

    def post_reply(self, *, channel: str, ts: str, text: str) -> RawAPIDict:
        _ = channel, ts, text
        return {}

    def open_dm(self, user_id: str) -> str:
        _ = user_id
        return ""

    def get_permalink(self, *, channel: str, ts: str) -> str:
        _ = channel, ts
        return ""

    def react(self, *, channel: str, ts: str, emoji: str) -> RawAPIDict:
        self.react_calls.append((channel, ts, emoji))
        return {"ok": True}

    def resolve_user_id(self, handle: str) -> str:
        _ = handle
        return ""


def _reaction_event(
    *,
    user: str = USER,
    name: str = "red_circle",
    channel: str = CHANNEL,
    ts: str = AGENT_TS,
    event_ts: str = SIGNAL_TS,
) -> RawAPIDict:
    return {
        "type": "reaction_added",
        "user": user,
        "reaction": name,
        "item": {"type": "message", "channel": channel, "ts": ts},
        "event_ts": event_ts,
    }


def _dm_event(*, text: str, user: str = USER, channel: str = DM_CHANNEL, ts: str = SIGNAL_TS) -> RawAPIDict:
    return {
        "type": "message",
        "user": user,
        "channel": channel,
        "text": text,
        "ts": ts,
    }


def _agent_message(text: str = "Sure, I'll do X") -> RawAPIDict:
    return {"text": text, "ts": AGENT_TS, "channel": CHANNEL, "user": "B0BOT"}


class TestRedCircleReaction:
    def test_red_circle_creates_row_and_emits_signal(self) -> None:
        backend = FakeMessaging(
            reactions=[_reaction_event(name="red_circle")],
            messages_by_ts={(CHANNEL, AGENT_TS): _agent_message()},
        )
        scanner = RedCardScanner(backend=backend, overlay="teatree")

        signals = scanner.scan()

        assert [s.kind for s in signals] == ["red_card.signal"]
        payload = signals[0].payload
        assert payload["signal_kind"] == RedCardSignal.Kind.RED_CIRCLE.value
        assert payload["user_id"] == USER
        assert payload["channel"] == CHANNEL
        assert payload["overlay"] == "teatree"
        assert payload["offending_message_ts"] == AGENT_TS
        rows = list(RedCardSignal.objects.all())
        assert len(rows) == 1
        assert rows[0].signal_kind == RedCardSignal.Kind.RED_CIRCLE
        assert rows[0].offending_message_text == "Sure, I'll do X"

    def test_red_circle_posts_eyes_on_user_signal(self) -> None:
        backend = FakeMessaging(
            reactions=[_reaction_event(name="red_circle")],
            messages_by_ts={(CHANNEL, AGENT_TS): _agent_message()},
        )

        RedCardScanner(backend=backend, overlay="teatree").scan()

        # :eyes: lands on the agent message the user red-carded — the user
        # sees the ack on the same message they reacted to.
        assert backend.react_calls == [(CHANNEL, AGENT_TS, "eyes")]
        row = RedCardSignal.objects.get()
        assert row.state == RedCardSignal.State.EYES_ADDED

    def test_other_user_red_circle_is_ignored(self) -> None:
        backend = FakeMessaging(
            reactions=[_reaction_event(name="red_circle", user=OTHER_USER)],
            messages_by_ts={(CHANNEL, AGENT_TS): _agent_message()},
        )

        signals = RedCardScanner(backend=backend, overlay="teatree").scan()

        assert signals == []
        assert RedCardSignal.objects.count() == 0

    def test_unrelated_reaction_is_ignored(self) -> None:
        backend = FakeMessaging(
            reactions=[_reaction_event(name="thumbsup")],
            messages_by_ts={(CHANNEL, AGENT_TS): _agent_message()},
        )

        signals = RedCardScanner(backend=backend, overlay="teatree").scan()

        assert signals == []
        assert RedCardSignal.objects.count() == 0


class TestNoEntrySignReaction:
    def test_no_entry_sign_creates_row_and_emits_signal(self) -> None:
        backend = FakeMessaging(
            reactions=[_reaction_event(name="no_entry_sign")],
            messages_by_ts={(CHANNEL, AGENT_TS): _agent_message()},
        )

        signals = RedCardScanner(backend=backend, overlay="teatree").scan()

        assert [s.kind for s in signals] == ["red_card.signal"]
        row = RedCardSignal.objects.get()
        assert row.signal_kind == RedCardSignal.Kind.NO_ENTRY_SIGN
        assert backend.react_calls == [(CHANNEL, AGENT_TS, "eyes")]


class TestRedCardText:
    def test_literal_red_card_in_dm_creates_text_signal(self) -> None:
        backend = FakeMessaging(dms=[_dm_event(text="RED CARD — that was wrong")])
        scanner = RedCardScanner(backend=backend, overlay="teatree")

        signals = scanner.scan()

        assert [s.kind for s in signals] == ["red_card.signal"]
        row = RedCardSignal.objects.get()
        assert row.signal_kind == RedCardSignal.Kind.RED_CARD_TEXT
        assert row.signal_text == "RED CARD — that was wrong"
        assert row.channel == DM_CHANNEL

    def test_case_insensitive_match(self) -> None:
        backend = FakeMessaging(dms=[_dm_event(text="this is a red card situation")])

        signals = RedCardScanner(backend=backend, overlay="teatree").scan()

        assert len(signals) == 1
        assert RedCardSignal.objects.count() == 1

    def test_red_dash_card_matches(self) -> None:
        backend = FakeMessaging(dms=[_dm_event(text="red-card!")])

        signals = RedCardScanner(backend=backend, overlay="teatree").scan()

        assert len(signals) == 1

    def test_redcard_one_word_does_not_match(self) -> None:
        backend = FakeMessaging(dms=[_dm_event(text="redcard")])

        signals = RedCardScanner(backend=backend, overlay="teatree").scan()

        assert signals == []
        assert RedCardSignal.objects.count() == 0

    def test_word_red_alone_does_not_match(self) -> None:
        backend = FakeMessaging(dms=[_dm_event(text="the button is red")])

        signals = RedCardScanner(backend=backend, overlay="teatree").scan()

        assert signals == []
        assert RedCardSignal.objects.count() == 0

    def test_text_signal_posts_eyes_on_user_message(self) -> None:
        backend = FakeMessaging(dms=[_dm_event(text="RED CARD")])

        RedCardScanner(backend=backend, overlay="teatree").scan()

        assert backend.react_calls == [(DM_CHANNEL, SIGNAL_TS, "eyes")]


class TestIdempotency:
    def test_same_reaction_seen_twice_yields_one_row_and_one_signal(self) -> None:
        backend = FakeMessaging(
            reactions=[_reaction_event(name="red_circle")],
            messages_by_ts={(CHANNEL, AGENT_TS): _agent_message()},
        )
        scanner = RedCardScanner(backend=backend, overlay="teatree")
        scanner.scan()

        # Re-queue the same event.
        backend.reactions = [_reaction_event(name="red_circle")]
        second = scanner.scan()

        assert second == []
        assert RedCardSignal.objects.count() == 1

    def test_same_dm_seen_twice_yields_one_row(self) -> None:
        backend = FakeMessaging(dms=[_dm_event(text="RED CARD")])
        scanner = RedCardScanner(backend=backend, overlay="teatree")
        scanner.scan()
        backend.dms = [_dm_event(text="RED CARD")]
        second = scanner.scan()

        assert second == []
        assert RedCardSignal.objects.count() == 1


class TestOverlayScoping:
    def test_default_overlay_is_empty_string(self) -> None:
        backend = FakeMessaging(dms=[_dm_event(text="RED CARD")])
        RedCardScanner(backend=backend).scan()
        row = RedCardSignal.objects.get()
        assert row.overlay == ""

    def test_different_overlays_with_same_signal_are_distinct(self) -> None:
        backend_a = FakeMessaging(dms=[_dm_event(text="RED CARD")])
        backend_b = FakeMessaging(dms=[_dm_event(text="RED CARD")])
        RedCardScanner(backend=backend_a, overlay="ovA").scan()
        RedCardScanner(backend=backend_b, overlay="ovB").scan()

        assert RedCardSignal.objects.count() == 2


class TestEventShapeEdges:
    """Defensive branches for malformed Slack events."""

    def test_reaction_without_item_is_skipped(self) -> None:
        backend = FakeMessaging(reactions=[{"type": "reaction_added", "user": USER, "reaction": "red_circle"}])
        signals = RedCardScanner(backend=backend, overlay="teatree").scan()
        assert signals == []

    def test_reaction_with_empty_item_is_skipped(self) -> None:
        backend = FakeMessaging(
            reactions=[{"type": "reaction_added", "user": USER, "reaction": "red_circle", "item": {}}]
        )
        signals = RedCardScanner(backend=backend, overlay="teatree").scan()
        assert signals == []

    def test_dm_without_ts_is_skipped(self) -> None:
        backend = FakeMessaging(dms=[{"text": "RED CARD", "user": USER, "channel": DM_CHANNEL}])
        signals = RedCardScanner(backend=backend, overlay="teatree").scan()
        assert signals == []

    def test_dm_without_text_is_skipped(self) -> None:
        backend = FakeMessaging(dms=[{"text": "", "user": USER, "channel": DM_CHANNEL, "ts": SIGNAL_TS}])
        signals = RedCardScanner(backend=backend, overlay="teatree").scan()
        assert signals == []

    def test_backend_without_fetch_reactions_emits_no_reaction_signals(self) -> None:
        """A messaging backend that doesn't expose ``fetch_reactions`` skips the reaction drain.

        Protects the scanner against backends that only support DM /
        mention flows — the reaction surface is best-effort.
        """

        class NoReactBackend:
            user_id = USER

            def fetch_dms(self, *, since: str = "") -> list[RawAPIDict]:
                _ = since
                return []

            def fetch_message(self, *, channel: str, ts: str) -> RawAPIDict:
                _ = channel, ts
                return {}

            def react(self, *, channel: str, ts: str, emoji: str) -> RawAPIDict:
                _ = channel, ts, emoji
                return {}

        signals = RedCardScanner(backend=NoReactBackend(), overlay="teatree").scan()
        assert signals == []
        assert RedCardSignal.objects.count() == 0

    def test_reaction_missing_agent_message_falls_back_to_empty_text(self) -> None:
        backend = FakeMessaging(reactions=[_reaction_event(name="red_circle")])
        signals = RedCardScanner(backend=backend, overlay="teatree").scan()

        assert len(signals) == 1
        row = RedCardSignal.objects.get()
        assert row.offending_message_text == ""

    def test_reaction_without_event_ts_falls_back_to_agent_ts(self) -> None:
        """Slack events without ``event_ts`` fall back to the agent message's ``ts``.

        The fallback keeps the row idempotency key well-defined — single
        row, no crash on the missing field.
        """
        event: RawAPIDict = {
            "type": "reaction_added",
            "user": USER,
            "reaction": "red_circle",
            "item": {"type": "message", "channel": CHANNEL, "ts": AGENT_TS},
        }
        backend = FakeMessaging(
            reactions=[event],
            messages_by_ts={(CHANNEL, AGENT_TS): _agent_message()},
        )

        signals = RedCardScanner(backend=backend, overlay="teatree").scan()

        assert len(signals) == 1
        row = RedCardSignal.objects.get()
        assert row.slack_ts == AGENT_TS

    def test_eyes_post_failure_does_not_raise(self) -> None:
        class _SlackDownError(RuntimeError):
            pass

        class FailingReact(FakeMessaging):
            def react(self, *, channel: str, ts: str, emoji: str) -> RawAPIDict:
                _ = channel, ts, emoji
                raise _SlackDownError

        backend = FailingReact(dms=[_dm_event(text="RED CARD")])

        signals = RedCardScanner(backend=backend, overlay="teatree").scan()

        # Signal is still emitted; the row is persisted even though the
        # :eyes: post failed (state stays PENDING).
        assert len(signals) == 1
        row = RedCardSignal.objects.get()
        assert row.state == RedCardSignal.State.PENDING


class TestScannerName:
    def test_scanner_name_for_logging(self) -> None:
        backend = FakeMessaging()
        scanner = RedCardScanner(backend=backend, overlay="teatree")
        assert "red_card" in scanner.name
