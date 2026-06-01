"""Tests for the configured-overlays anchor in the statusline.

The statusline should surface which overlays are configured so the user
sees their multi-overlay context at a glance, instead of overlays only
appearing implicitly when a ticket or PR happens to carry an ``[ov]``
prefix.
"""

import tempfile
from pathlib import Path
from unittest.mock import patch

import django.test

from teatree.loop.dispatch import DispatchAction
from teatree.loop.rendering import zones_for
from teatree.loop.rendering_zones import _MAX_PER_STATE
from teatree.loop.statusline import overlays_anchor, render
from teatree.loop.tick import TickRequest, run_tick


class TestOverlaysAnchor:
    """Pure formatter: configured overlay names render one dim summary line."""

    def test_lists_configured_overlays(self) -> None:
        with patch(
            "teatree.loop.statusline._configured_overlay_names",
            return_value=["alpha-portal", "t3-acme", "t3-teatree"],
        ):
            assert overlays_anchor() == ["overlays: alpha-portal · t3-acme · t3-teatree"]

    def test_empty_when_no_overlays(self) -> None:
        with patch("teatree.loop.statusline._configured_overlay_names", return_value=[]):
            assert overlays_anchor() == []

    def test_fails_open_on_error(self) -> None:
        with patch(
            "teatree.loop.statusline._configured_overlay_names",
            side_effect=RuntimeError("config broken"),
        ):
            assert overlays_anchor() == []


class TestOverlaysAnchorWiring(django.test.TestCase):
    """``zones_for`` and the empty-jobs tick path both surface the overlays line."""

    def test_zones_for_includes_overlays_anchor(self) -> None:
        with patch(
            "teatree.loop.statusline._configured_overlay_names",
            return_value=["alpha", "beta"],
        ):
            zones = zones_for([], colorize=False)
        anchor_text = "\n".join(item if isinstance(item, str) else item.text for item in zones.anchors)
        assert "overlays: alpha · beta" in anchor_text, anchor_text

    def test_empty_jobs_path_renders_overlays_anchor(self) -> None:
        with (
            tempfile.TemporaryDirectory() as d,
            patch(
                "teatree.loop.statusline._configured_overlay_names",
                return_value=["alpha", "beta"],
            ),
        ):
            sl = Path(d) / "sl.txt"
            run_tick(TickRequest(scanners=[]), statusline_path=sl, colorize=False)
            assert "overlays: alpha · beta" in sl.read_text(encoding="utf-8")


class TestOverlaysAnchorSurvivesPendingTaskFlood(django.test.TestCase):
    """A backlog of statusline-fallback rows must not bury the overlays line.

    Auto-enqueued ``short_describe`` tasks have no phase agent, so each
    ``pending_task`` falls through ``dispatch`` to an in-flight statusline
    row with no ``[ov]`` prefix (``_ClassifiedActions.other``). Uncapped,
    one tick's worth floods the in-flight zone and pushes the configured-
    overlays anchor out of the height-limited statusline pane.
    """

    @staticmethod
    def _flood(count: int) -> list[DispatchAction]:
        return [
            DispatchAction(
                kind="statusline",
                zone="in_flight",
                detail=f"Task {i} (short_describe) pending",
                payload={},
            )
            for i in range(1, count + 1)
        ]

    def test_overlays_line_present_under_pending_task_flood(self) -> None:
        with (
            tempfile.TemporaryDirectory() as d,
            patch(
                "teatree.loop.statusline._configured_overlay_names",
                return_value=["alpha", "beta"],
            ),
        ):
            zones = zones_for(self._flood(50), colorize=False)
            sl = Path(d) / "sl.txt"
            render(zones, target=sl, colorize=False)
            body = sl.read_text(encoding="utf-8")
        assert "overlays: alpha · beta" in body, body
        assert body.count("(short_describe) pending") <= _MAX_PER_STATE, body
        assert "more)" in body, body

    def test_flood_does_not_blow_past_visible_window(self) -> None:
        with patch(
            "teatree.loop.statusline._configured_overlay_names",
            return_value=["alpha", "beta"],
        ):
            zones = zones_for(self._flood(50), colorize=False)
        assert len(zones.in_flight) <= _MAX_PER_STATE + 1, zones.in_flight
