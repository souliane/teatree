"""The global-health chip in the statusline anchors zone (PR-17).

``health_chip`` renders the read-only operational-health verdict as a colored
status dot plus the open-issue count. It reads persisted state (never a
reconcile at render time) and fails open to ``[]``.
"""

import tempfile
from datetime import datetime, timedelta
from pathlib import Path
from unittest.mock import patch

import pytest
from django.utils import timezone

from teatree.core.factory.operational_health import HealthReport, HealthStatus
from teatree.core.models.known_issue import KnownIssue
from teatree.loop.rendering import zones_for
from teatree.loop.statusline import health_chip
from teatree.loop.statusline_palette import _ANSI_GREEN, _ANSI_RED
from teatree.loop.tick import TickRequest, run_tick


def _report(
    status: HealthStatus,
    count: int,
    *,
    seen: datetime | None = None,
    measured_at: datetime | None = None,
) -> HealthReport:
    issues = tuple(
        KnownIssue(fingerprint=f"f{i}", severity="warning", summary="s", last_seen=seen or timezone.now())
        for i in range(count)
    )
    return HealthReport(status=status, open_issues=issues, measured_at=measured_at or timezone.now())


class TestHealthChipFormatter:
    def test_green_clean_renders_dot_no_count(self) -> None:
        with patch("teatree.core.factory.operational_health.read_health", return_value=_report(HealthStatus.GREEN, 0)):
            assert health_chip(colorize=False) == ["health: ● (now)"]

    def test_open_count_appended(self) -> None:
        with patch("teatree.core.factory.operational_health.read_health", return_value=_report(HealthStatus.RED, 3)):
            assert health_chip(colorize=False) == ["health: ● 3 (now)"]

    def test_the_verdict_carries_the_age_it_was_measured_at(self) -> None:
        """A verdict with no date on it is read as current — this is the whole point.

        The chip renders PERSISTED state that only a reconcile refreshes, so when the
        loop that reconciles is stopped an hours-old verdict sits there looking live.
        """
        stale = _report(HealthStatus.RED, 2, seen=timezone.now() - timedelta(hours=3))
        with patch("teatree.core.factory.operational_health.read_health", return_value=stale):
            assert health_chip(colorize=False) == ["health: ● 2 (3h)"]

    def test_a_clean_verdict_is_dated_from_the_last_reconcile(self) -> None:
        """Green is the verdict whose age matters most: it has no issue to date it."""
        aged = _report(HealthStatus.GREEN, 0, measured_at=timezone.now() - timedelta(days=2))
        with patch("teatree.core.factory.operational_health.read_health", return_value=aged):
            assert health_chip(colorize=False) == ["health: ● (2d)"]

    def test_an_undateable_verdict_says_unknown_rather_than_now(self) -> None:
        """Anti-vacuity: the stamp must not silently round an unknown age down to fresh."""
        undated = HealthReport(status=HealthStatus.GREEN, open_issues=(), measured_at=None)
        with patch("teatree.core.factory.operational_health.read_health", return_value=undated):
            assert health_chip(colorize=False) == ["health: ● (age?)"]

    def test_colorize_wraps_dot_in_verdict_color(self) -> None:
        with patch("teatree.core.factory.operational_health.read_health", return_value=_report(HealthStatus.RED, 2)):
            red = health_chip(colorize=True)[0]
        with patch("teatree.core.factory.operational_health.read_health", return_value=_report(HealthStatus.GREEN, 0)):
            green = health_chip(colorize=True)[0]
        assert _ANSI_RED in red
        assert _ANSI_GREEN in green

    def test_fails_open_on_read_error(self) -> None:
        with patch("teatree.core.factory.operational_health.read_health", side_effect=RuntimeError("boom")):
            assert health_chip(colorize=False) == []


# ast-grep-ignore: ac-django-no-pytest-django-db
@pytest.mark.django_db
class TestHealthChipWiring:
    def test_zones_for_includes_health_chip(self) -> None:
        with patch("teatree.core.factory.operational_health.read_health", return_value=_report(HealthStatus.YELLOW, 1)):
            zones = zones_for([], colorize=False)
        anchor_text = "\n".join(item if isinstance(item, str) else item.text for item in zones.anchors)
        assert "health: ● 1" in anchor_text, anchor_text

    def test_empty_jobs_tick_renders_health_chip(self) -> None:
        with (
            tempfile.TemporaryDirectory() as d,
            patch("teatree.core.factory.operational_health.read_health", return_value=_report(HealthStatus.YELLOW, 2)),
        ):
            sl = Path(d) / "sl.txt"
            run_tick(TickRequest(scanners=[]), statusline_path=sl, colorize=False)
            assert "health: ● 2" in sl.read_text(encoding="utf-8")
