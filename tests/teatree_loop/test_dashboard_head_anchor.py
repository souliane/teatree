"""The consolidated dashboard head line (loops + availability + overlays + health).

The owner asked for overlays and health to stop each wasting a whole row:
``dashboard_head_anchor`` folds the live-loops line (which already carries the
availability segment, #1678), the configured-overlays summary, and the
global-health chip onto ONE line. These regression-lock that overlays and
health ride the same first line as the loops, never separate rows.
"""

import tempfile
from datetime import UTC, datetime, timedelta
from pathlib import Path
from unittest.mock import patch

import pytest

from teatree.core.factory.operational_health import HealthReport, HealthStatus
from teatree.core.models.known_issue import KnownIssue
from teatree.loop.rendering import zones_for
from teatree.loop.statusline import dashboard_head_anchor, render
from teatree.loop.tick import TickRequest, run_tick


def _health(status: HealthStatus, count: int) -> HealthReport:
    issues = tuple(KnownIssue(fingerprint=f"f{i}", severity="warning", summary="s") for i in range(count))
    return HealthReport(status=status, open_issues=issues)


def _live_lease() -> list[tuple[str, datetime]]:
    return [("loop-tick", datetime.now(UTC) - timedelta(seconds=60))]


class TestDashboardHeadAnchor:
    """Pure formatter: the four segments fold onto exactly one line."""

    def test_folds_loops_availability_overlays_health_onto_one_line(self) -> None:
        with (
            patch("teatree.loop.statusline_loops._live_loop_leases", return_value=_live_lease()),
            patch("teatree.loop.statusline_loops._cadence_for_loop", return_value=720),
            patch("teatree.loop.statusline_loops._availability_segment", return_value="availability: away (override)"),
            patch("teatree.loop.statusline_loops._configured_overlay_names", return_value=["t3-teatree"]),
            patch("teatree.core.factory.operational_health.read_health", return_value=_health(HealthStatus.RED, 3)),
        ):
            lines = dashboard_head_anchor(colorize=False)
        assert len(lines) == 1, repr(lines)
        line = lines[0]
        assert "tick 11m" in line, line
        assert "availability: away (override)" in line, line
        assert "overlays: t3-teatree" in line, line
        assert "health: ● 3" in line, line

    def test_no_loops_still_folds_overlays_and_health_onto_one_line(self) -> None:
        with (
            patch("teatree.loop.statusline_loops._live_loop_leases", return_value=[]),
            patch("teatree.loop.statusline_loops._configured_overlay_names", return_value=["alpha", "beta"]),
            patch("teatree.core.factory.operational_health.read_health", return_value=_health(HealthStatus.GREEN, 0)),
        ):
            lines = dashboard_head_anchor(colorize=False)
        assert lines == ["overlays: alpha · beta · health: ●"], repr(lines)

    def test_empty_when_nothing_to_show(self) -> None:
        with (
            patch("teatree.loop.statusline_loops._live_loop_leases", return_value=[]),
            patch("teatree.loop.statusline_loops._configured_overlay_names", return_value=[]),
            patch("teatree.core.factory.operational_health.read_health", side_effect=RuntimeError("boom")),
        ):
            assert dashboard_head_anchor(colorize=False) == []

    def test_fails_open_per_segment(self) -> None:
        # A broken overlays read drops only its segment; loops + health survive.
        with (
            patch("teatree.loop.statusline_loops._live_loop_leases", return_value=_live_lease()),
            patch("teatree.loop.statusline_loops._cadence_for_loop", return_value=720),
            patch("teatree.loop.statusline_loops._availability_segment", return_value=""),
            patch(
                "teatree.loop.statusline_loops._configured_overlay_names",
                side_effect=RuntimeError("config broken"),
            ),
            patch("teatree.core.factory.operational_health.read_health", return_value=_health(HealthStatus.GREEN, 0)),
        ):
            lines = dashboard_head_anchor(colorize=False)
        assert len(lines) == 1, repr(lines)
        assert "overlays:" not in lines[0], lines[0]
        assert "health: ●" in lines[0], lines[0]


# ast-grep-ignore: ac-django-no-pytest-django-db
@pytest.mark.django_db
class TestDashboardHeadConsolidatesRows:
    """overlays and health no longer occupy their own zones rows."""

    def _overlays_and_health_row_count(self, body: str) -> tuple[int, int]:
        overlay_rows = sum(1 for ln in body.splitlines() if "overlays:" in ln)
        health_rows = sum(1 for ln in body.splitlines() if "health: ●" in ln)
        return overlay_rows, health_rows

    def test_zones_for_puts_overlays_health_on_the_loop_line(self, tmp_path: Path) -> None:
        with (
            patch("teatree.loop.statusline_loops._live_loop_leases", return_value=_live_lease()),
            patch("teatree.loop.statusline_loops._cadence_for_loop", return_value=720),
            patch("teatree.loop.statusline_loops._configured_overlay_names", return_value=["alpha", "beta"]),
            patch("teatree.core.factory.operational_health.read_health", return_value=_health(HealthStatus.YELLOW, 1)),
        ):
            zones = zones_for([], colorize=False)
            target = tmp_path / "sl.txt"
            render(zones, target=target, colorize=False)
        body = target.read_text(encoding="utf-8")
        loop_lines = [ln for ln in body.splitlines() if "tick 11m" in ln]
        assert len(loop_lines) == 1, body
        # The overlays + health segments ride that single loop line.
        assert "overlays: alpha · beta" in loop_lines[0], body
        assert "health: ● 1" in loop_lines[0], body
        # And they do not also appear on their own separate rows.
        assert self._overlays_and_health_row_count(body) == (1, 1), body

    def test_empty_jobs_tick_folds_overlays_health_onto_loop_line(self) -> None:
        with (
            tempfile.TemporaryDirectory() as d,
            patch("teatree.loop.statusline_loops._live_loop_leases", return_value=_live_lease()),
            patch("teatree.loop.statusline_loops._cadence_for_loop", return_value=720),
            patch("teatree.loop.statusline_loops._configured_overlay_names", return_value=["alpha"]),
            patch("teatree.core.factory.operational_health.read_health", return_value=_health(HealthStatus.RED, 2)),
        ):
            sl = Path(d) / "sl.txt"
            run_tick(TickRequest(scanners=[]), statusline_path=sl, colorize=False)
            body = sl.read_text(encoding="utf-8")
        loop_lines = [ln for ln in body.splitlines() if "tick 11m" in ln]
        assert len(loop_lines) == 1, body
        assert "overlays: alpha" in loop_lines[0], body
        assert "health: ● 2" in loop_lines[0], body
