"""Tests for :class:`SlackReviewIntentScanner` — reaction-driven review loop (#1047).

The scanner watches Slack reactions and mentions for the configured user, on
messages that reference an MR/PR URL. Each fresh trigger creates one
:class:`ReviewAssignment` row (idempotent on ``(overlay, mr_url, user_id)``)
and emits one ``slack.review_intent`` signal that the dispatcher routes to
the ``t3:reviewer`` agent.

No ``:eyes:`` claim reaction is posted at discovery on either the reaction or
mention path: a claim reaction belongs to review-DONE, never to start
(#113/#86). The review-intent signals are suppressed entirely while the
review loop is stopped (#79).
"""

from dataclasses import dataclass, field

import pytest

from teatree.core.models import ReviewAssignment, ReviewIntent
from teatree.loop.scanners.slack_review_intent import SlackReviewIntentScanner
from teatree.types import RawAPIDict

pytestmark = pytest.mark.django_db


MR = "https://gitlab.com/owner/repo/-/merge_requests/42"
USER = "U0DEMOUSER1"
OTHER_USER = "UOTHER"
CHANNEL = "C09D25ZHCRJ"
TS = "1779180558.938799"


@dataclass
class FakeMessaging:
    """In-memory MessagingBackend for scanner tests."""

    user_id: str = USER
    reactions: list[RawAPIDict] = field(default_factory=list)
    mentions: list[RawAPIDict] = field(default_factory=list)
    messages_by_ts: dict[tuple[str, str], RawAPIDict] = field(default_factory=dict)
    existing_reactions: dict[tuple[str, str], list[str]] = field(default_factory=dict)
    react_calls: list[tuple[str, str, str]] = field(default_factory=list)

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


def _reaction_event(*, user: str = USER, name: str = "thumbsup", channel: str = CHANNEL, ts: str = TS) -> RawAPIDict:
    return {
        "type": "reaction_added",
        "user": user,
        "reaction": name,
        "item": {"type": "message", "channel": channel, "ts": ts},
        "event_ts": ts,
    }


def _mention_event(
    *, user: str = USER, text: str = f"review {MR} please", channel: str = CHANNEL, ts: str = TS
) -> RawAPIDict:
    return {
        "type": "app_mention",
        "user": user,
        "text": text,
        "channel": channel,
        "ts": ts,
    }


def _message(text: str = f"please review {MR}") -> RawAPIDict:
    return {"text": text, "ts": TS, "channel": CHANNEL}


class TestReactionDriven:
    def test_user_reaction_on_mr_message_creates_assignment_and_signal(self) -> None:
        backend = FakeMessaging(
            reactions=[_reaction_event()],
            messages_by_ts={(CHANNEL, TS): _message()},
        )
        scanner = SlackReviewIntentScanner(backend=backend, overlay="teatree")

        signals = scanner.scan()

        assert [s.kind for s in signals] == ["slack.review_intent"]
        assert signals[0].payload["mr_url"] == MR
        assert signals[0].payload["trigger"] == "reaction"
        rows = list(ReviewAssignment.objects.all())
        assert len(rows) == 1
        assert rows[0].mr_url == MR
        assert rows[0].user_id == USER
        assert rows[0].trigger == "reaction"

    def test_other_user_reaction_is_ignored(self) -> None:
        backend = FakeMessaging(
            reactions=[_reaction_event(user=OTHER_USER)],
            messages_by_ts={(CHANNEL, TS): _message()},
        )

        signals = SlackReviewIntentScanner(backend=backend, overlay="teatree").scan()

        assert signals == []
        assert ReviewAssignment.objects.count() == 0

    def test_reaction_on_message_without_mr_url_is_skipped(self) -> None:
        backend = FakeMessaging(
            reactions=[_reaction_event()],
            messages_by_ts={(CHANNEL, TS): _message(text="just a hello")},
        )

        signals = SlackReviewIntentScanner(backend=backend, overlay="teatree").scan()

        assert signals == []
        assert ReviewAssignment.objects.count() == 0

    def test_reaction_does_not_post_eyes(self) -> None:
        backend = FakeMessaging(
            reactions=[_reaction_event()],
            messages_by_ts={(CHANNEL, TS): _message()},
        )

        SlackReviewIntentScanner(backend=backend, overlay="teatree").scan()

        # The user already reacted — no need for the bot to ack with :eyes:.
        assert backend.react_calls == []

    def test_same_mr_reacted_twice_is_idempotent(self) -> None:
        backend = FakeMessaging(
            reactions=[_reaction_event(name="thumbsup"), _reaction_event(name="raised_hands", ts="2.0")],
            messages_by_ts={(CHANNEL, TS): _message(), (CHANNEL, "2.0"): _message()},
        )

        signals = SlackReviewIntentScanner(backend=backend, overlay="teatree").scan()

        # Same (overlay, mr_url, user_id) — second reaction is a no-op.
        assert len(signals) == 1
        assert ReviewAssignment.objects.count() == 1


class TestMentionDriven:
    def test_user_mention_on_mr_message_creates_assignment_without_eyes_claim(self) -> None:
        # #113/#86: a mention records the review intent and emits the dispatch
        # signal, but posts NO :eyes: claim reaction at discovery — the claim
        # reaction belongs to review-DONE. The row stays PENDING.
        backend = FakeMessaging(mentions=[_mention_event()])
        scanner = SlackReviewIntentScanner(backend=backend, overlay="teatree")

        signals = scanner.scan()

        assert [s.kind for s in signals] == ["slack.review_intent"]
        assert signals[0].payload["mr_url"] == MR
        assert signals[0].payload["trigger"] == "mention"
        assert backend.react_calls == []
        row = ReviewAssignment.objects.get()
        assert row.state == ReviewAssignment.State.PENDING

    def test_mention_without_mr_url_is_skipped(self) -> None:
        backend = FakeMessaging(mentions=[_mention_event(text="hello agent")])

        signals = SlackReviewIntentScanner(backend=backend, overlay="teatree").scan()

        assert signals == []
        assert ReviewAssignment.objects.count() == 0
        assert backend.react_calls == []

    def test_mention_when_user_already_reacted_skips_eyes_but_records_assignment(self) -> None:
        # Both a reaction event AND a mention event for the same MR.
        # The reaction fires first (creates assignment); the mention
        # sees an existing assignment and stays a no-op for :eyes:.
        backend = FakeMessaging(
            reactions=[_reaction_event()],
            mentions=[_mention_event()],
            messages_by_ts={(CHANNEL, TS): _message()},
        )

        signals = SlackReviewIntentScanner(backend=backend, overlay="teatree").scan()

        assert [s.kind for s in signals] == ["slack.review_intent"]
        assert backend.react_calls == []  # user already reacted — no :eyes:
        assert ReviewAssignment.objects.count() == 1
        # Trigger is the first one we saw (reaction).
        assert ReviewAssignment.objects.get().trigger == "reaction"


class TestOverlayScoping:
    def test_different_overlays_with_same_mr_user_are_distinct(self) -> None:
        backend_a = FakeMessaging(
            reactions=[_reaction_event()],
            messages_by_ts={(CHANNEL, TS): _message()},
        )
        backend_b = FakeMessaging(
            reactions=[_reaction_event()],
            messages_by_ts={(CHANNEL, TS): _message()},
        )

        SlackReviewIntentScanner(backend=backend_a, overlay="ovA").scan()
        SlackReviewIntentScanner(backend=backend_b, overlay="ovB").scan()

        assert ReviewAssignment.objects.count() == 2


class TestApprovalReactionApi:
    """Requirement 3: white_check_mark when t3 approves an MR the user reviewed.

    The hot path is already wired via ``add_approval_reaction`` (signals.py).
    We assert the dedicated helper ``approve_review_assignment`` advances the
    ledger row to ``approved`` so the audit trail captures the closed loop.
    """

    def test_approve_review_assignment_marks_row_approved(self) -> None:
        row = ReviewAssignment.record(
            ReviewIntent(mr_url=MR, user_id=USER, channel=CHANNEL, slack_ts=TS, trigger="reaction")
        )
        assert row is not None

        from teatree.loop.scanners.slack_review_intent import approve_review_assignment  # noqa: PLC0415

        count = approve_review_assignment(mr_url=MR, overlay="")
        assert count == 1
        row.refresh_from_db()
        assert row.state == ReviewAssignment.State.APPROVED
        assert row.approved_at is not None

    def test_approve_review_assignment_is_idempotent(self) -> None:
        row = ReviewAssignment.record(
            ReviewIntent(mr_url=MR, user_id=USER, channel=CHANNEL, slack_ts=TS, trigger="reaction")
        )
        assert row is not None

        from teatree.loop.scanners.slack_review_intent import approve_review_assignment  # noqa: PLC0415

        approve_review_assignment(mr_url=MR, overlay="")
        second = approve_review_assignment(mr_url=MR, overlay="")
        assert second == 0


class TestScannerName:
    def test_scanner_name_for_logging(self) -> None:
        backend = FakeMessaging()
        scanner = SlackReviewIntentScanner(backend=backend, overlay="teatree")
        assert "slack_review_intent" in scanner.name


class TestEventShapeEdges:
    """Defensive branches for malformed Slack events.

    Real Slack payloads always carry these fields, but the scanner runs on
    untyped dicts so each branch needs a regression test — otherwise a
    refactor that drops a guard silently passes coverage with a misleading
    100% number.
    """

    def test_reaction_event_without_item_dict_is_skipped(self) -> None:
        # Slack always wraps the message ref in an ``item`` dict; if a
        # malformed event arrives without one, the scanner must skip it.
        event: RawAPIDict = {"type": "reaction_added", "user": USER}
        backend = FakeMessaging(reactions=[event])

        signals = SlackReviewIntentScanner(backend=backend, overlay="teatree").scan()

        assert signals == []

    def test_reaction_event_with_empty_item_is_skipped(self) -> None:
        # ``item={}`` has no channel/ts — guard returns ``("", "")`` and
        # the scanner bails before touching the message.
        event: RawAPIDict = {"type": "reaction_added", "user": USER, "item": {}}
        backend = FakeMessaging(reactions=[event])

        signals = SlackReviewIntentScanner(backend=backend, overlay="teatree").scan()

        assert signals == []

    def test_reaction_when_fetch_message_returns_non_dict_is_skipped(self) -> None:
        # If ``fetch_message`` returns something weird (e.g. ``[]`` because
        # the backend mis-shaped the response), the scanner must not crash.
        backend = FakeMessaging(reactions=[_reaction_event()])
        # Don't seed ``messages_by_ts`` — ``fetch_message`` returns ``{}`` →
        # ``text`` becomes ``""`` → no MR URL → no signal.
        signals = SlackReviewIntentScanner(backend=backend, overlay="teatree").scan()

        assert signals == []

    def test_mention_with_non_string_text_is_skipped(self) -> None:
        # Defensive guard in ``record_mention_intent`` for malformed events.
        event: RawAPIDict = {"text": 42, "channel": CHANNEL, "ts": TS, "type": "app_mention"}
        backend = FakeMessaging(mentions=[event])

        signals = SlackReviewIntentScanner(backend=backend, overlay="teatree").scan()

        assert signals == []

    def test_mention_with_empty_text_is_skipped(self) -> None:
        event: RawAPIDict = {"text": "", "channel": CHANNEL, "ts": TS, "type": "app_mention"}
        backend = FakeMessaging(mentions=[event])

        signals = SlackReviewIntentScanner(backend=backend, overlay="teatree").scan()

        assert signals == []

    def test_mention_when_backend_has_no_user_id_is_skipped(self) -> None:
        # Without ``user_id`` on the backend, we can't attribute the
        # intent to anyone, so the row must not be created.
        backend = FakeMessaging(user_id="", mentions=[_mention_event()])

        signals = SlackReviewIntentScanner(backend=backend, overlay="teatree").scan()

        assert signals == []
        assert ReviewAssignment.objects.count() == 0

    def test_mention_eyes_failure_is_swallowed(self) -> None:
        # ``backend.react`` may raise transient errors (rate-limit, 5xx).
        # The row still records, the signal still fires; only :eyes:
        # is best-effort.
        from teatree.core.models import ReviewAssignment  # noqa: PLC0415

        @dataclass
        class FlakyMessaging(FakeMessaging):
            def react(self, *, channel: str, ts: str, emoji: str) -> RawAPIDict:
                _ = channel, ts, emoji
                msg = "slack 5xx"
                raise RuntimeError(msg)

        backend = FlakyMessaging(mentions=[_mention_event()])
        signals = SlackReviewIntentScanner(backend=backend, overlay="teatree").scan()

        assert [s.kind for s in signals] == ["slack.review_intent"]
        # Row exists but state stayed pending because the eyes post raised.
        row = ReviewAssignment.objects.get()
        assert row.state == ReviewAssignment.State.PENDING


class TestProductionDrainPath:
    """The reaction-queue JSONL drain is the production hot path.

    Tests above use the in-memory ``fetch_reactions`` path. This test
    exercises ``drain_reactions_queue`` reading from a real JSONL file
    so a refactor that breaks the production drain doesn't ship green.
    """

    def test_drains_reaction_events_from_jsonl_queue(self, tmp_path, monkeypatch) -> None:
        import json  # noqa: PLC0415

        from teatree.backends import slack_receiver  # noqa: PLC0415

        queue = tmp_path / "slack-reactions.jsonl"
        event = _reaction_event()
        queue.write_text(json.dumps({"overlay": "teatree", "event": event}) + "\n", encoding="utf-8")

        # Redirect ``drain_reactions_queue`` to read from this tmp file.
        def fake_default() -> "object":
            return queue

        monkeypatch.setattr(slack_receiver, "default_reactions_queue_path", fake_default)

        backend = FakeMessaging(messages_by_ts={(CHANNEL, TS): _message()})
        signals = SlackReviewIntentScanner(backend=backend, overlay="teatree").scan()

        assert [s.kind for s in signals] == ["slack.review_intent"]
        # File was atomically renamed, consumed, and committed after persist.
        assert not queue.is_file()
        assert not queue.with_suffix(".draining").is_file()

    def test_reaction_queue_recovers_after_crash_before_persist(self, tmp_path, monkeypatch) -> None:
        import json  # noqa: PLC0415

        from teatree.backends import slack_receiver  # noqa: PLC0415

        queue = tmp_path / "slack-reactions.jsonl"
        event = _reaction_event()
        queue.write_text(json.dumps({"overlay": "teatree", "event": event}) + "\n", encoding="utf-8")

        def fake_default() -> "object":
            return queue

        monkeypatch.setattr(slack_receiver, "default_reactions_queue_path", fake_default)

        # First drain reads the event but the process "crashes" before commit:
        # drain_reactions_queue leaves the .draining file in place.
        drained = slack_receiver.drain_reactions_queue()
        assert len(drained) == 1

        # Next scan must recover the reaction rather than lose it.
        backend = FakeMessaging(messages_by_ts={(CHANNEL, TS): _message()})
        signals = SlackReviewIntentScanner(backend=backend, overlay="teatree").scan()
        assert [s.kind for s in signals] == ["slack.review_intent"]
        assert not queue.with_suffix(".draining").is_file()


class TestBackendWithoutMentionsApi:
    """Backends without ``fetch_mentions`` skip the mention path cleanly.

    Production ``MessagingBackend`` (slack_bot) has fetch_mentions, but
    minimal noop / DM-only backends don't — the scanner must degrade
    gracefully rather than crash on ``AttributeError``.
    """

    def test_scanner_skips_mention_drain_when_backend_lacks_fetch_mentions(self) -> None:
        @dataclass
        class ReactionsOnlyBackend(FakeMessaging):
            fetch_mentions: None = None  # shadow the inherited callable with a non-callable

        backend = ReactionsOnlyBackend(reactions=[_reaction_event()], messages_by_ts={(CHANNEL, TS): _message()})
        signals = SlackReviewIntentScanner(backend=backend, overlay="teatree").scan()

        # Reaction path still works; mention path silently no-ops.
        assert [s.kind for s in signals] == ["slack.review_intent"]
