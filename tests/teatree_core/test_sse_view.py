import asyncio
import tempfile
import time
from pathlib import Path
from unittest.mock import patch

import pytest
from django.test import Client, TestCase
from django.urls import reverse

from teatree.core.views.sse import _PANEL_BUILDERS, DashboardSSEView, _detect_changed_panels, _format_sse


class TestDetectChangedPanels(TestCase):
    def _mock_builders(self, values: dict[str, str] | None = None):
        """Patch panel builders to return deterministic values."""
        defaults = {panel: f"{panel}_v1" for panel in _PANEL_BUILDERS}
        if values:
            defaults.update(values)
        return {panel: lambda v=v: v for panel, v in defaults.items()}

    def test_returns_all_panels_on_first_check(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
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

    def test_returns_empty_when_mtime_unchanged(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            db_file = tmp_path / "db.sqlite3"
            db_file.write_text("")
            mtime = db_file.stat().st_mtime
            with patch("teatree.core.views.sse.settings") as mock_settings:
                mock_settings.DATABASES = {"default": {"NAME": str(db_file)}}
                changed, returned_mtime, hashes = _detect_changed_panels(mtime, {})
            assert changed == []
            assert returned_mtime == mtime
            assert hashes == {}

    def test_returns_empty_when_file_missing(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            with patch("teatree.core.views.sse.settings") as mock_settings:
                mock_settings.DATABASES = {"default": {"NAME": str(tmp_path / "nonexistent.db")}}
                changed, mtime, hashes = _detect_changed_panels(0.0, {})
            assert changed == []
            assert mtime == pytest.approx(0.0)
            assert hashes == {}

    def test_only_changed_panels_returned(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
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

    def test_detects_multiple_changed_panels(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
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


class TestFormatSSE(TestCase):
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


class TestDashboardSSEView(TestCase):
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

    def test_max_duration_terminates_stream(self):
        """TEATREE_SSE_MAX_DURATION causes stream to end after the set duration."""

        def fake_detect(last_mtime, last_hashes):
            return [], last_mtime, last_hashes

        view = DashboardSSEView()
        view._poll_interval = 0.01
        with (
            patch("teatree.core.views.sse._detect_changed_panels", side_effect=fake_detect),
            patch("teatree.core.views.sse.settings") as mock_settings,
        ):
            mock_settings.TEATREE_SSE_MAX_DURATION = 0.05
            chunks = _collect_stream(view)

        # Stream should have terminated (not loop forever)
        assert len(chunks) >= 1
        assert chunks[0] == b'event: connected\ndata: {"status": "ok"}\n\n'

    def test_max_duration_expires_during_detect(self):
        """Stream exits via the pre-sleep check when detect takes long enough."""
        real_monotonic = time.monotonic
        call_count = 0
        base = real_monotonic()

        def advancing_monotonic():
            """First calls return base, then jump past max_duration."""
            nonlocal call_count
            call_count += 1
            # Calls 1-2: started + top-of-loop check (within budget)
            if call_count <= 2:
                return base
            # Call 3: pre-sleep check — past the deadline
            return base + 10.0

        def fake_detect(last_mtime, last_hashes):
            return [], last_mtime, last_hashes

        view = DashboardSSEView()
        view._poll_interval = 0.01
        with (
            patch("teatree.core.views.sse._detect_changed_panels", side_effect=fake_detect),
            patch("teatree.core.views.sse.settings") as mock_settings,
            patch("teatree.core.views.sse.time") as mock_time,
        ):
            mock_settings.TEATREE_SSE_MAX_DURATION = 1.0
            mock_time.monotonic = advancing_monotonic
            chunks = _collect_stream(view)

        assert chunks[0] == b'event: connected\ndata: {"status": "ok"}\n\n'
        # Stream terminated after just one detect iteration (pre-sleep break)
        assert len(chunks) == 1

    def test_max_duration_zero_means_unlimited(self):
        """When TEATREE_SSE_MAX_DURATION is 0 (default), stream runs until cancelled."""
        call_count = 0

        def fake_detect(last_mtime, last_hashes):
            nonlocal call_count
            call_count += 1
            if call_count > 3:
                raise asyncio.CancelledError
            return [], last_mtime, last_hashes

        view = DashboardSSEView()
        view._poll_interval = 0.01
        with (
            patch("teatree.core.views.sse._detect_changed_panels", side_effect=fake_detect),
            patch("teatree.core.views.sse.settings") as mock_settings,
        ):
            mock_settings.TEATREE_SSE_MAX_DURATION = 0
            _collect_stream(view)

        # Should have run until CancelledError (4 iterations)
        assert call_count == 4
