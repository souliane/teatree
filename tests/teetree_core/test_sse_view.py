import asyncio
import time
from unittest.mock import patch

import pytest
from django.test import Client
from django.urls import reverse

from teetree.core.views.sse import _ALL_PANELS, DashboardSSEView, _detect_changes, _format_sse

pytestmark = pytest.mark.django_db


class TestDetectChanges:
    def test_returns_all_panels_when_mtime_increases(self, tmp_path):
        db_file = tmp_path / "db.sqlite3"
        db_file.write_text("")
        with patch("teetree.core.views.sse.settings") as mock_settings:
            mock_settings.DATABASES = {"default": {"NAME": str(db_file)}}
            changed, new_mtime = _detect_changes(0.0)
        assert changed == list(_ALL_PANELS)
        assert new_mtime > 0.0

    def test_returns_empty_when_mtime_unchanged(self, tmp_path):
        db_file = tmp_path / "db.sqlite3"
        db_file.write_text("")
        mtime = db_file.stat().st_mtime
        with patch("teetree.core.views.sse.settings") as mock_settings:
            mock_settings.DATABASES = {"default": {"NAME": str(db_file)}}
            changed, returned_mtime = _detect_changes(mtime)
        assert changed == []
        assert returned_mtime == mtime

    def test_returns_empty_when_file_missing(self, tmp_path):
        with patch("teetree.core.views.sse.settings") as mock_settings:
            mock_settings.DATABASES = {"default": {"NAME": str(tmp_path / "nonexistent.db")}}
            changed, mtime = _detect_changes(0.0)
        assert changed == []
        assert mtime == pytest.approx(0.0)

    def test_detects_second_change(self, tmp_path):
        db_file = tmp_path / "db.sqlite3"
        db_file.write_text("")
        with patch("teetree.core.views.sse.settings") as mock_settings:
            mock_settings.DATABASES = {"default": {"NAME": str(db_file)}}
            _, first_mtime = _detect_changes(0.0)
            time.sleep(0.05)
            db_file.write_text("changed")
            changed, second_mtime = _detect_changes(first_mtime)
        assert changed == list(_ALL_PANELS)
        assert second_mtime > first_mtime


class TestFormatSSE:
    def test_formats_event_correctly(self):
        result = _format_sse("summary", {"panel": "summary"})
        assert result == b'event: summary\ndata: {"panel": "summary"}\n\n'

    def test_returns_bytes(self):
        result = _format_sse("test", {})
        assert isinstance(result, bytes)


def _collect_stream(view: DashboardSSEView) -> list[bytes]:
    """Run the async event stream and collect all chunks."""

    async def _run():
        return [chunk async for chunk in view._event_stream()]

    return asyncio.run(_run())


class TestDashboardSSEView:
    def test_returns_event_stream_headers(self):
        call_count = 0

        def fake_detect(last_mtime):
            nonlocal call_count
            call_count += 1
            raise asyncio.CancelledError

        with patch("teetree.core.views.sse._detect_changes", side_effect=fake_detect):
            response = Client().get(reverse("teetree:dashboard-events"))
        assert response["Content-Type"] == "text/event-stream"
        assert response["Cache-Control"] == "no-cache"
        assert response["X-Accel-Buffering"] == "no"

    def test_first_event_is_connected(self):
        def fake_detect(last_mtime):
            raise asyncio.CancelledError

        view = DashboardSSEView()
        with patch("teetree.core.views.sse._detect_changes", side_effect=fake_detect):
            chunks = _collect_stream(view)
        assert chunks[0] == b'event: connected\ndata: {"status": "ok"}\n\n'

    def test_emits_panel_events_on_change(self):
        call_count = 0

        def fake_detect(last_mtime):
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                return ["summary", "sessions"], 1.0
            raise asyncio.CancelledError

        view = DashboardSSEView()
        view._poll_interval = 0.01
        with patch("teetree.core.views.sse._detect_changes", side_effect=fake_detect):
            chunks = _collect_stream(view)

        all_data = b"".join(chunks)
        assert b"event: summary" in all_data
        assert b"event: sessions" in all_data

    def test_emits_heartbeat_when_idle(self):
        call_count = 0

        def fake_detect(last_mtime):
            nonlocal call_count
            call_count += 1
            if call_count > 5:
                raise asyncio.CancelledError
            return [], last_mtime

        view = DashboardSSEView()
        view._poll_interval = 0.01
        view._heartbeat_every = 3
        with patch("teetree.core.views.sse._detect_changes", side_effect=fake_detect):
            chunks = _collect_stream(view)

        all_data = b"".join(chunks)
        assert b": heartbeat" in all_data

    def test_heartbeat_resets_after_data_event(self):
        call_count = 0

        def fake_detect(last_mtime):
            nonlocal call_count
            call_count += 1
            if call_count == 3:
                return ["summary"], 1.0
            if call_count > 8:
                raise asyncio.CancelledError
            return [], last_mtime

        view = DashboardSSEView()
        view._poll_interval = 0.01
        view._heartbeat_every = 4
        with patch("teetree.core.views.sse._detect_changes", side_effect=fake_detect):
            chunks = _collect_stream(view)

        all_data = b"".join(chunks)
        assert b"event: summary" in all_data
        # With heartbeat_every=4 and a data event at tick 3 resetting the counter,
        # we need 4 more idle ticks after that. With 8 total ticks and event at 3,
        # we get 5 idle ticks after → should have a heartbeat.
        assert b": heartbeat" in all_data
