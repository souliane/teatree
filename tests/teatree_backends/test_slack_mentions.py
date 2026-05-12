"""Tests for the SlackMentionsScanner (Socket Mode queue + API merge)."""

import json
from pathlib import Path
from unittest.mock import MagicMock, patch

from teatree.loop.scanners.slack_mentions import SlackMentionsScanner


class TestSlackMentionsScanner:
    def _make_backend(self, *, mentions: list | None = None, dms: list | None = None) -> MagicMock:
        backend = MagicMock()
        backend.fetch_mentions.return_value = mentions or []
        backend.fetch_dms.return_value = dms or []
        return backend

    def test_surfaces_api_mentions(self, tmp_path: Path) -> None:
        backend = self._make_backend(mentions=[{"ts": "1.0", "text": "hey @bot"}])
        scanner = SlackMentionsScanner(backend=backend, cursor_path=tmp_path / "cursor.json")

        signals = scanner.scan()

        assert len(signals) == 1
        assert signals[0].kind == "slack.mention"
        assert "hey @bot" in signals[0].summary

    def test_surfaces_api_dms(self, tmp_path: Path) -> None:
        backend = self._make_backend(dms=[{"ts": "2.0", "text": "dm text"}])
        scanner = SlackMentionsScanner(backend=backend, cursor_path=tmp_path / "cursor.json")

        signals = scanner.scan()

        assert len(signals) == 1
        assert signals[0].kind == "slack.dm"

    def test_merges_socket_mode_queue_events(self, tmp_path: Path) -> None:
        backend = self._make_backend()
        scanner = SlackMentionsScanner(backend=backend, cursor_path=tmp_path / "cursor.json")
        queued_events = [
            {"event": {"type": "app_mention", "ts": "3.0", "text": "queued mention"}},
            {"event": {"type": "message", "channel_type": "im", "ts": "4.0", "text": "queued dm"}},
        ]
        with patch("teatree.backends.slack_receiver.drain_event_queue", return_value=queued_events):
            signals = scanner.scan()

        assert len(signals) == 2
        kinds = {s.kind for s in signals}
        assert kinds == {"slack.mention", "slack.dm"}

    def test_ignores_non_im_queued_messages(self, tmp_path: Path) -> None:
        backend = self._make_backend()
        scanner = SlackMentionsScanner(backend=backend, cursor_path=tmp_path / "cursor.json")
        queued = [{"event": {"type": "message", "channel_type": "channel", "ts": "5.0", "text": "not im"}}]
        with patch("teatree.backends.slack_receiver.drain_event_queue", return_value=queued):
            signals = scanner.scan()

        assert len(signals) == 0

    def test_updates_cursors_on_success(self, tmp_path: Path) -> None:
        backend = self._make_backend(mentions=[{"ts": "10.0", "text": "x"}])
        cursor_path = tmp_path / "cursor.json"
        scanner = SlackMentionsScanner(backend=backend, cursor_path=cursor_path)

        scanner.scan()

        data = json.loads(cursor_path.read_text(encoding="utf-8"))
        assert data["mentions"] == "10.0"

    def test_empty_scan_does_not_write_cursors(self, tmp_path: Path) -> None:
        backend = self._make_backend()
        cursor_path = tmp_path / "cursor.json"
        scanner = SlackMentionsScanner(backend=backend, cursor_path=cursor_path)

        with patch("teatree.backends.slack_receiver.drain_event_queue", return_value=[]):
            scanner.scan()

        assert not cursor_path.is_file()

    def test_reads_existing_cursors(self, tmp_path: Path) -> None:
        backend = self._make_backend()
        cursor_path = tmp_path / "cursor.json"
        cursor_path.write_text('{"mentions": "5.0", "dms": "3.0"}', encoding="utf-8")
        scanner = SlackMentionsScanner(backend=backend, cursor_path=cursor_path)

        scanner.scan()

        backend.fetch_mentions.assert_called_once_with(since="5.0")
        backend.fetch_dms.assert_called_once_with(since="3.0")
