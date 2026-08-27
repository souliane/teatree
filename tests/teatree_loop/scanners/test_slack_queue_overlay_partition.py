"""A shared Slack queue drain consumes only what the draining scanner owns.

The Socket Mode receiver writes every overlay's events into ONE
``slack-events.jsonl`` / ``slack-reactions.jsonl`` pair and tags each record
with the overlay it arrived for. Both consumers read only ``record["event"]``,
so in a multi-overlay tick whichever scanner drained first processed every
sibling's events under its own overlay tag and backend — and then unlinked the
backing file, so the owning scanner saw an empty queue.

The same unconditional commit also destroyed an event whose handler raised:
per-item fault isolation skipped it, the file was committed anyway.

These tests drive the real scanners against the real JSONL queue under a real
temporary directory; only the Slack API surface is faked.
"""

import json
from dataclasses import dataclass, field
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Any
from unittest.mock import patch

from django.test import TestCase

from teatree.backends.slack import receiver
from teatree.loop.scanners.slack_mentions import SlackMentionsScanner
from teatree.loop.scanners.slack_review_intent import SlackReviewIntentScanner
from teatree.types import RawAPIDict

MR_URL = "https://gitlab.example.com/team/project/-/merge_requests/77"
CHANNEL = "C_REVIEW"
USER = "U_SELF"


@dataclass
class FakeMessaging:
    """Messaging surface the two drain-owning scanners touch."""

    user_id: str = USER
    messages_by_ts: dict[tuple[str, str], RawAPIDict] = field(default_factory=dict)
    reacted: list[tuple[str, str, str]] = field(default_factory=list)

    def fetch_mentions(self, since: str = "") -> list[RawAPIDict]:
        _ = since
        return []

    def fetch_dms(self, since: str = "") -> list[RawAPIDict]:
        _ = since
        return []

    def fetch_message(self, *, channel: str, ts: str) -> RawAPIDict:
        return self.messages_by_ts.get((channel, ts), {})

    def get_permalink(self, *, channel: str, ts: str) -> str:
        return f"https://slack.example.com/{channel}/{ts}"

    def react(self, *, channel: str, ts: str, emoji: str) -> RawAPIDict:
        self.reacted.append((channel, ts, emoji))
        return {"ok": True}

    def react_routed(self, *, channel: str, ts: str, emoji: str, **_kwargs: Any) -> RawAPIDict:
        return self.react(channel=channel, ts=ts, emoji=emoji)


def _write_queue(path: Path, records: list[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        "".join(json.dumps(record, separators=(",", ":")) + "\n" for record in records),
        encoding="utf-8",
    )


def _queued_records(path: Path) -> list[dict[str, Any]]:
    if not path.is_file():
        return []
    return [json.loads(line) for line in path.read_text(encoding="utf-8").strip().splitlines() if line]


def _mention(ts: str) -> RawAPIDict:
    return {"type": "app_mention", "ts": ts, "channel": CHANNEL, "text": f"<@{USER}> review {MR_URL}"}


def _reaction(ts: str) -> RawAPIDict:
    return {
        "type": "reaction_added",
        "user": USER,
        "reaction": "eyes",
        "item": {"type": "message", "channel": CHANNEL, "ts": ts},
        "event_ts": ts,
    }


class _QueueDirMixin(TestCase):
    """Gives each test its own on-disk queue directory."""

    def queue_path(self, filename: str) -> Path:
        return Path(self.enterContext(TemporaryDirectory())) / filename


class TestMentionsQueueOverlayPartition(_QueueDirMixin):
    """``SlackMentionsScanner`` leaves a sibling overlay's queued mentions alone."""

    def test_sibling_overlay_mention_is_neither_consumed_nor_destroyed(self) -> None:
        queue = self.queue_path("slack-events.jsonl")
        _write_queue(
            queue,
            [
                {"overlay": "alpha", "event": _mention("1.0001")},
                {"overlay": "beta", "event": _mention("2.0002")},
            ],
        )
        scanner = SlackMentionsScanner(
            backend=FakeMessaging(),
            cursor_path=queue.parent / "cursor.json",
            overlay="alpha",
        )

        with patch.object(receiver, "default_queue_path", return_value=queue):
            signals = scanner.scan()

        assert [signal.payload["ts"] for signal in signals if signal.kind == "slack.mention"] == ["1.0001"]
        assert [record["overlay"] for record in _queued_records(queue)] == ["beta"]

    def test_untagged_legacy_record_is_still_consumed(self) -> None:
        queue = self.queue_path("slack-events.jsonl")
        _write_queue(queue, [{"overlay": "", "event": _mention("1.0001")}])
        scanner = SlackMentionsScanner(
            backend=FakeMessaging(),
            cursor_path=queue.parent / "cursor.json",
            overlay="alpha",
        )

        with patch.object(receiver, "default_queue_path", return_value=queue):
            signals = scanner.scan()

        assert [signal.payload["ts"] for signal in signals if signal.kind == "slack.mention"] == ["1.0001"]
        assert _queued_records(queue) == []


class TestReactionsQueueOverlayPartition(_QueueDirMixin):
    """``SlackReviewIntentScanner`` leaves a sibling overlay's queued reactions alone."""

    def test_sibling_overlay_reaction_survives_the_drain(self) -> None:
        queue = self.queue_path("slack-reactions.jsonl")
        _write_queue(
            queue,
            [
                {"overlay": "alpha", "event": _reaction("1.0001")},
                {"overlay": "beta", "event": _reaction("2.0002")},
            ],
        )
        backend = FakeMessaging(
            messages_by_ts={
                (CHANNEL, "1.0001"): {"text": f"please review {MR_URL}"},
                (CHANNEL, "2.0002"): {"text": f"please review {MR_URL}"},
            },
        )

        with patch.object(receiver, "default_reactions_queue_path", return_value=queue):
            signals = SlackReviewIntentScanner(backend=backend, overlay="alpha").scan()

        assert [signal.payload["ts"] for signal in signals] == ["1.0001"]
        assert [record["overlay"] for record in _queued_records(queue)] == ["beta"]


class TestFailedReactionIsNotDestroyed(_QueueDirMixin):
    """A reaction whose handler raised is re-queued, not committed away."""

    @staticmethod
    def _raise(*_args: object, **_kwargs: object) -> None:
        msg = "simulated handler failure"
        raise RuntimeError(msg)

    def test_raising_reaction_is_requeued_for_the_next_drain(self) -> None:
        queue = self.queue_path("slack-reactions.jsonl")
        _write_queue(queue, [{"overlay": "alpha", "event": _reaction("1.0001")}])

        with (
            patch.object(receiver, "default_reactions_queue_path", return_value=queue),
            patch.object(SlackReviewIntentScanner, "_handle_reaction", self._raise),
        ):
            assert SlackReviewIntentScanner(backend=FakeMessaging(), overlay="alpha").scan() == []

        retained = _queued_records(queue)
        assert [record["overlay"] for record in retained] == ["alpha"]
        assert retained[0]["attempts"] == 1

    def test_repeatedly_failing_reaction_is_dropped_once_the_budget_is_spent(self) -> None:
        queue = self.queue_path("slack-reactions.jsonl")
        _write_queue(queue, [{"overlay": "alpha", "attempts": 2, "event": _reaction("1.0001")}])

        with (
            patch.object(receiver, "default_reactions_queue_path", return_value=queue),
            patch.object(SlackReviewIntentScanner, "_handle_reaction", self._raise),
        ):
            SlackReviewIntentScanner(backend=FakeMessaging(), overlay="alpha").scan()

        assert _queued_records(queue) == []
