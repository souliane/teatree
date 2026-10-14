"""Tests for the SlackMentionsScanner (backend mention queue + the Socket Mode JSONL queue)."""

from unittest.mock import MagicMock, patch

from teatree.loop.scanners.base import ScanSignal
from teatree.loop.scanners.slack_mentions import SlackMentionsScanner
from teatree.types import RawAPIDict


def _queued_dm(**fields: str) -> dict[str, RawAPIDict]:
    return {"event": {"type": "message", "channel_type": "im", **fields}}


def _scan_draining(scanner: SlackMentionsScanner, queued: list[dict[str, RawAPIDict]]) -> list[ScanSignal]:
    with patch("teatree.backends.slack.receiver.drain_event_queue", return_value=queued):
        return scanner.scan()


class TestSlackMentionsScanner:
    def _make_backend(self, *, mentions: list | None = None) -> MagicMock:
        backend = MagicMock()
        backend.fetch_mentions.return_value = mentions or []
        # An unresolved bot identity makes the drained-DM recorder fail closed, so no test here touches the DB.
        backend.auth_test.return_value = {"ok": False}
        return backend

    def test_surfaces_api_mentions(self) -> None:
        backend = self._make_backend(mentions=[{"ts": "1.0", "text": "hey @bot"}])
        scanner = SlackMentionsScanner(backend=backend)

        signals = scanner.scan()

        assert len(signals) == 1
        assert signals[0].kind == "slack.mention"
        assert "hey @bot" in signals[0].summary

    def test_merges_socket_mode_queue_events(self) -> None:
        backend = self._make_backend()
        scanner = SlackMentionsScanner(backend=backend)
        queued_events = [
            {"event": {"type": "app_mention", "ts": "3.0", "text": "queued mention"}},
            _queued_dm(ts="4.0", text="queued dm"),
        ]

        signals = _scan_draining(scanner, queued_events)

        assert len(signals) == 2
        kinds = {s.kind for s in signals}
        assert kinds == {"slack.mention", "slack.dm"}

    def test_ignores_non_im_queued_messages(self) -> None:
        backend = self._make_backend()
        scanner = SlackMentionsScanner(backend=backend)
        queued = [{"event": {"type": "message", "channel_type": "channel", "ts": "5.0", "text": "not im"}}]

        signals = _scan_draining(scanner, queued)

        assert len(signals) == 0

    def test_fetches_mentions_with_no_since_cursor(self) -> None:
        backend = self._make_backend()
        scanner = SlackMentionsScanner(backend=backend)

        _scan_draining(scanner, [])

        backend.fetch_mentions.assert_called_once_with()

    def test_dm_signal_carries_permalink(self) -> None:
        """Each ``slack.dm`` signal carries the message permalink (#1050).

        Renderer-side the line is `[ov] DMs (N): <permalink1> · …` —
        resolving the permalink lazily at render time would hit Slack
        once per tick per DM. The scanner enriches once at scan time
        and caches the URL in the signal payload.
        """
        backend = self._make_backend()
        backend.get_permalink.return_value = "https://slk.example/archives/D123/p2000000"
        scanner = SlackMentionsScanner(backend=backend)

        signals = _scan_draining(scanner, [_queued_dm(ts="2.0", text="dm text", channel="D123")])

        assert len(signals) == 1
        assert signals[0].kind == "slack.dm"
        assert signals[0].payload.get("permalink") == "https://slk.example/archives/D123/p2000000"
        backend.get_permalink.assert_called_once_with(channel="D123", ts="2.0")

    def test_dm_signal_permalink_empty_on_lookup_failure(self) -> None:
        """When ``get_permalink`` raises or returns empty, the signal still emits.

        Renderer falls back to the bare ``ts`` as label — Slack outages
        must not break statusline rendering.
        """
        backend = self._make_backend()
        backend.get_permalink.side_effect = RuntimeError("api down")
        scanner = SlackMentionsScanner(backend=backend)

        signals = _scan_draining(scanner, [_queued_dm(ts="2.0", text="dm text", channel="D123")])

        assert len(signals) == 1
        assert signals[0].payload.get("permalink") == ""

    def test_dm_signal_permalink_skipped_when_channel_missing(self) -> None:
        """Signals without ``event.channel`` get an empty permalink.

        When ``channel`` is missing the backend is not called — there's
        nothing to resolve.
        """
        backend = self._make_backend()
        scanner = SlackMentionsScanner(backend=backend)

        signals = _scan_draining(scanner, [_queued_dm(ts="2.0", text="dm text")])

        assert signals[0].payload.get("permalink") == ""
        backend.get_permalink.assert_not_called()
