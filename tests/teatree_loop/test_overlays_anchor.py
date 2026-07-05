"""The configured-overlays anchor in the statusline (PR-17 restore of #1663).

The statusline surfaces which overlays are configured so the user sees their
multi-overlay context at a glance, instead of overlays only appearing
implicitly when a ticket or PR happens to carry an ``[ov]`` prefix.
"""

import tempfile
from pathlib import Path
from unittest.mock import patch

import pytest

from teatree.loop.rendering import zones_for
from teatree.loop.statusline import overlays_anchor
from teatree.loop.tick import TickRequest, run_tick


class TestOverlaysAnchor:
    """Pure formatter: configured overlay names render one dim summary line."""

    def test_lists_configured_overlays(self) -> None:
        with patch(
            "teatree.loop.statusline_loops._configured_overlay_names",
            return_value=["alpha-portal", "t3-acme", "t3-teatree"],
        ):
            assert overlays_anchor() == ["overlays: alpha-portal · t3-acme · t3-teatree"]

    def test_empty_when_no_overlays(self) -> None:
        with patch("teatree.loop.statusline_loops._configured_overlay_names", return_value=[]):
            assert overlays_anchor() == []

    def test_fails_open_on_error(self) -> None:
        with patch(
            "teatree.loop.statusline_loops._configured_overlay_names",
            side_effect=RuntimeError("config broken"),
        ):
            assert overlays_anchor() == []


# ast-grep-ignore: ac-django-no-pytest-django-db
@pytest.mark.django_db
class TestOverlaysAnchorWiring:
    """``zones_for`` and the empty-jobs tick path both surface the overlays line."""

    def test_zones_for_includes_overlays_anchor(self) -> None:
        with patch(
            "teatree.loop.statusline_loops._configured_overlay_names",
            return_value=["alpha", "beta"],
        ):
            zones = zones_for([], colorize=False)
        anchor_text = "\n".join(item if isinstance(item, str) else item.text for item in zones.anchors)
        assert "overlays: alpha · beta" in anchor_text, anchor_text

    def test_empty_jobs_path_renders_overlays_anchor(self) -> None:
        with (
            tempfile.TemporaryDirectory() as d,
            patch(
                "teatree.loop.statusline_loops._configured_overlay_names",
                return_value=["alpha", "beta"],
            ),
        ):
            sl = Path(d) / "sl.txt"
            run_tick(TickRequest(scanners=[]), statusline_path=sl, colorize=False)
            assert "overlays: alpha · beta" in sl.read_text(encoding="utf-8")
