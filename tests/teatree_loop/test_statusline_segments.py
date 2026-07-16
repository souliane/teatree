"""Contributed inline statusline segments in ``tick-meta.json`` (#3237).

The single hardcoded ``cost_chip`` is generalized into a registry of named
segments: core and overlays contribute segments, ``_write_tick_meta`` assembles
them into ``tick-meta.json``'s ``segments`` list, and ``statusline.sh`` splices
each at its placement. ``cost_chip`` becomes the first (``usage``-placed)
registry entry produced by core; the dedicated ``cost_chip`` key is retired.
"""

import datetime as dt
import json
from pathlib import Path
from unittest import mock

import pytest
from django.utils import timezone

from teatree.core.models import Session, Task, TaskAttempt
from teatree.core.statusline_segment import StatuslineSegment
from teatree.loop.tick_freshness import _write_tick_meta
from tests.factories import TicketFactory

# ast-grep-ignore: ac-django-no-pytest-django-db
pytestmark = pytest.mark.django_db


def _meta(tmp_path: Path) -> dict:
    statusline = tmp_path / "statusline.txt"
    _write_tick_meta(dt.datetime(2026, 6, 10, tzinfo=dt.UTC), target=statusline)
    return json.loads((tmp_path / "tick-meta.json").read_text(encoding="utf-8"))


class TestSegmentDataContract:
    def test_as_meta_omits_absent_color(self) -> None:
        seg = StatuslineSegment(id="x", text="⇡12", placement="header")
        assert seg.as_meta() == {"id": "x", "text": "⇡12", "placement": "header"}

    def test_as_meta_carries_color_when_set(self) -> None:
        seg = StatuslineSegment(id="x", text="⇡12", color="yellow", placement="header")
        assert seg.as_meta()["color"] == "yellow"

    def test_default_placement_is_header(self) -> None:
        assert StatuslineSegment(id="x", text="t").placement == "header"


class TestSegmentsInTickMeta:
    def setup_method(self) -> None:
        self.ticket = TicketFactory()
        self.session = Session.objects.create(ticket=self.ticket)
        self.task = Task.objects.create(ticket=self.ticket, session=self.session)

    def _headless(self, cost: float) -> None:
        TaskAttempt.objects.create(
            task=self.task,
            execution_target=Task.ExecutionTarget.HEADLESS,
            cost_usd=cost,
            started_at=timezone.now(),
        )

    def test_cost_chip_is_the_first_usage_segment(self, tmp_path: Path) -> None:
        self._headless(48.0)
        segments = _meta(tmp_path)["segments"]
        assert segments[0]["id"] == "cost_chip"
        assert segments[0]["placement"] == "usage"
        assert segments[0]["text"] == "SDK mtd ≈$48/$200"

    def test_dedicated_cost_chip_key_is_retired(self, tmp_path: Path) -> None:
        self._headless(48.0)
        assert "cost_chip" not in _meta(tmp_path)

    def test_no_cost_segment_when_no_headless_spend(self, tmp_path: Path) -> None:
        assert all(s["id"] != "cost_chip" for s in _meta(tmp_path)["segments"])

    def test_overlay_segments_are_collected(self, tmp_path: Path) -> None:
        contributed = StatuslineSegment(id="upstream-behind", text="⇡12", color="yellow", placement="header")
        fake = mock.Mock()
        fake.get_statusline_segments.return_value = [contributed]
        with mock.patch("teatree.loop.tick_freshness._registered_overlays", return_value=[fake]):
            segments = _meta(tmp_path)["segments"]
        assert {"id": "upstream-behind", "text": "⇡12", "color": "yellow", "placement": "header"} in segments

    def test_broken_overlay_producer_fails_open(self, tmp_path: Path) -> None:
        self._headless(10.0)
        good = mock.Mock()
        good.get_statusline_segments.return_value = [StatuslineSegment(id="ok", text="ok", placement="header")]
        broken = mock.Mock()
        broken.get_statusline_segments.side_effect = RuntimeError("watcher cache missing")
        with mock.patch("teatree.loop.tick_freshness._registered_overlays", return_value=[broken, good]):
            segments = _meta(tmp_path)["segments"]
        ids = {s["id"] for s in segments}
        # A broken producer never blanks the line; the good ones still land.
        assert "cost_chip" in ids
        assert "ok" in ids
