import asyncio
import time
from unittest.mock import patch

import pytest
from django.test import Client
from django.urls import reverse

from teatree.core.views.sse import _PANEL_BUILDERS, DashboardSSEView, _detect_changed_panels, _format_sse

pytestmark = pytest.mark.django_db


class TestDetectChangedPanels:
    def _mock_builders(self, values: dict[str, str] | None = None):
        """Patch panel builders to return deterministic values."""
        defaults = {panel: f"{panel}_v1" for panel in _PANEL_BUILDERS}
        if values:
            defaults.update(values)
        return {panel: lambda v=v: v for panel, v in defaults.items()}

    def test_returns_all_panels_on_first_check(self, tmp_path):
        db_file = tmp_path / "db.sqlite3"
        db_file.write_text("")
        with (
            patch("teatree.core.views.sse.settings") as mock_settings,
            patch.dict("teatree.core.views.sse._PANEL_BUILDERS", self._mock_builders()),
        ):
            mock_settings.DATABASES = {"default": {"NAME": str(db_file)}}
            changed, new_mtime, hashes = _detect_changed_panels(0.0, {})
        assert set(changed) == set(_PANEL_BUILDERS.keys())
        assert new_mtime > 0.0
        assert len(hashes) == len(_PANEL_BUILDERS)

    def test_returns_empty_when_mtime_unchanged(self, tmp_path):
        db_file = tmp_path / "db.sqlite3"
        db_file.write_text("")
        mtime = db_file.stat().st_mtime
        with patch("teatree.core.views.sse.settings") as mock_settings:
            mock_settings.DATABASES = {"default": {"NAME": str(db_file)}}
            changed, returned_mtime, hashes = _detect_changed_panels(mtime, {})
        assert changed == []
        assert returned_mtime == mtime
        assert hashes == {}

    def test_returns_empty_when_file_missing(self, tmp_path):
        with patch("teatree.core.views.sse.settings") as mock_settings:
            mock_settings.DATABASES = {"default": {"NAME": str(tmp_path / "nonexistent.db")}}
            changed, mtime, hashes = _detect_changed_panels(0.0, {})
        assert changed == []
        assert mtime == pytest.approx(0.0)
        assert hashes == {}

    def test_only_changed_panels_returned(self, tmp_path):
        db_file = tmp_path / "db.sqlite3"
        db_file.write_text("")
        builders_v1 = self._mock_builders()
        with (
            patch("teatree.core.views.sse.settings") as mock_settings,
            patch.dict("teatree.core.views.sse._PANEL_BUILDERS", builders_v1),
        ):
            mock_settings.DATABASES = {"default": {"NAME": str(db_file)}}
            _, first_mtime, first_hashes = _detect_changed_panels(0.0, {})

            # Bump mtime but only change the "summary" panel
            time.sleep(0.05)
            db_file.write_text("changed")
            builders_v2 = self._mock_builders({"summary": "summary_v2"})
            with patch.dict("teatree.core.views.sse._PANEL_BUILDERS", builders_v2):
                changed, second_mtime, second_hashes = _detect_changed_panels(first_mtime, first_hashes)

        assert changed == ["summary"]
        assert second_mtime > first_mtime
        assert second_hashes["summary"] != first_hashes["summary"]

    def test_detects_multiple_changed_panels(self, tmp_path):
        db_file = tmp_path / "db.sqlite3"
        db_file.write_text("")
        builders_v1 = self._mock_builders()
        with (
            patch("teatree.core.views.sse.settings") as mock_settings,
            patch.dict("teatree.core.views.sse._PANEL_BUILDERS", builders_v1),
        ):
            mock_settings.DATABASES = {"default": {"NAME": str(db_file)}}
            _, first_mtime, first_hashes = _detect_changed_panels(0.0, {})

            time.sleep(0.05)
            db_file.write_text("changed again")
            builders_v2 = self._mock_builders({"summary": "summary_v2", "tickets": "tickets_v2"})
            with patch.dict("teatree.core.views.sse._PANEL_BUILDERS", builders_v2):
                changed, _, _ = _detect_changed_panels(first_mtime, first_hashes)

        assert set(changed) == {"summary", "tickets"}


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

        def fake_detect(last_mtime, last_hashes):
            nonlocal call_count
            call_count += 1
            raise asyncio.CancelledError

        with patch("teatree.core.views.sse._detect_changed_panels", side_effect=fake_detect):
            response = Client().get(reverse("teatree:dashboard-events"))
        assert response["Content-Type"] == "text/event-stream"
        assert response["Cache-Control"] == "no-cache"
        assert response["X-Accel-Buffering"] == "no"

    def test_first_event_is_connected(self):
        def fake_detect(last_mtime, last_hashes):
            raise asyncio.CancelledError

        view = DashboardSSEView()
        with patch("teatree.core.views.sse._detect_changed_panels", side_effect=fake_detect):
            chunks = _collect_stream(view)
        assert chunks[0] == b'event: connected\ndata: {"status": "ok"}\n\n'

    def test_emits_panel_events_on_change(self):
        call_count = 0

        def fake_detect(last_mtime, last_hashes):
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                return ["summary", "sessions"], 1.0, {"summary": "h1", "sessions": "h2"}
            raise asyncio.CancelledError

        view = DashboardSSEView()
        view._poll_interval = 0.01
        with patch("teatree.core.views.sse._detect_changed_panels", side_effect=fake_detect):
            chunks = _collect_stream(view)

        all_data = b"".join(chunks)
        assert b"event: summary" in all_data
        assert b"event: sessions" in all_data

    def test_emits_heartbeat_when_idle(self):
        call_count = 0

        def fake_detect(last_mtime, last_hashes):
            nonlocal call_count
            call_count += 1
            if call_count > 5:
                raise asyncio.CancelledError
            return [], last_mtime, last_hashes

        view = DashboardSSEView()
        view._poll_interval = 0.01
        view._heartbeat_every = 3
        with patch("teatree.core.views.sse._detect_changed_panels", side_effect=fake_detect):
            chunks = _collect_stream(view)

        all_data = b"".join(chunks)
        assert b": heartbeat" in all_data

    def test_heartbeat_resets_after_data_event(self):
        call_count = 0

        def fake_detect(last_mtime, last_hashes):
            nonlocal call_count
            call_count += 1
            if call_count == 3:
                return ["summary"], 1.0, {"summary": "h1"}
            if call_count > 8:
                raise asyncio.CancelledError
            return [], last_mtime, last_hashes

        view = DashboardSSEView()
        view._poll_interval = 0.01
        view._heartbeat_every = 4
        with patch("teatree.core.views.sse._detect_changed_panels", side_effect=fake_detect):
            chunks = _collect_stream(view)

        all_data = b"".join(chunks)
        assert b"event: summary" in all_data
        assert b": heartbeat" in all_data
