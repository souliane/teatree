"""Live and replayed events share one per-overlay queue memo."""

import json
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from teatree.backends.slack.receiver import DeliveredEvents, DeliveryContext, QueuePaths, deliver_event


def _context(tmp_path: Path, seen: DeliveredEvents, wake: MagicMock) -> DeliveryContext:
    return DeliveryContext(
        queues=QueuePaths(events=tmp_path / "events.jsonl", reactions=tmp_path / "reactions.jsonl"),
        identity=None,
        on_event=wake,
        seen=seen,
    )


def test_a_replayed_message_uses_the_live_queue_write_once(tmp_path: Path) -> None:
    seen = DeliveredEvents()
    wake = MagicMock()
    context = _context(tmp_path, seen, wake)
    event = {"type": "message", "channel_type": "im", "channel": "D1", "ts": "1.000100", "text": "hello"}

    assert deliver_event("demo", event, context=context)
    assert not deliver_event("demo", event, context=context)

    lines = context.queues.events.read_text(encoding="utf-8").splitlines()
    assert [json.loads(line)["event"] for line in lines] == [event]
    assert seen.as_mapping() == {"message:D1:1.000100": 1.0001}
    wake.assert_called_once_with("demo", event)


def test_a_failed_queue_write_does_not_claim_the_event(tmp_path: Path) -> None:
    seen = DeliveredEvents()
    event = {"type": "message", "channel": "D1", "ts": "1.0"}
    context = _context(tmp_path, seen, MagicMock())

    with (
        patch("teatree.backends.slack.receiver._enqueue", side_effect=OSError("disk full")),
        pytest.raises(OSError, match="disk full"),
    ):
        deliver_event("demo", event, context=context)

    assert seen.as_mapping() == {}
    assert deliver_event("demo", event, context=context)


def test_reaction_identity_includes_channel_timestamp_reaction_and_user() -> None:
    seen = DeliveredEvents()
    delivered = MagicMock(return_value=True)
    event = {"type": "reaction_added", "item": {"channel": "D1", "ts": "2.0"}, "reaction": "+1", "user": "U1"}

    assert seen.deliver_once(event, delivered)
    assert not seen.deliver_once(event, delivered)
    assert seen.deliver_once({**event, "user": "U2"}, delivered)
    seen.prune(before=3.0)

    assert seen.as_mapping() == {}
    assert DeliveredEvents.from_mapping({"message:D1:4.0": 4.0}).as_mapping() == {"message:D1:4.0": 4.0}
