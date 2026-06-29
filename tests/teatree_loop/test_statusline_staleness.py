"""Unit tests for the statusline render-age freshness gate.

The months-long stale-info bug: a frozen statusline (dead/stopped loop)
is displayed verbatim by both readers with no staleness signal. The gate
records ``rendered_at`` in the tick-meta sidecar and surfaces a RED banner
once the render age crosses ``max(2 * cadence, 300s)``.
"""

import json
from pathlib import Path

from teatree.loop.statusline_staleness import (
    FLOOR_SECONDS,
    STALE_CADENCE_MULTIPLIER,
    stale_cutoff_seconds,
    staleness_banner,
    staleness_banner_for,
)


class TestStaleCutoff:
    def test_uses_two_times_cadence_when_above_floor(self) -> None:
        # 2 * 720 = 1440 > 300 floor
        assert stale_cutoff_seconds(720) == STALE_CADENCE_MULTIPLIER * 720

    def test_floor_applies_for_short_cadence(self) -> None:
        # 2 * 60 = 120 < 300 floor → floor wins, so a 60s test loop does
        # not flag stale after a single skipped tick.
        assert stale_cutoff_seconds(60) == FLOOR_SECONDS


class TestStalenessBannerWording:
    def test_banner_names_age_and_remedy(self) -> None:
        banner = staleness_banner(6 * 3600, colorize=False)
        assert "STALE" in banner
        assert "6h ago" in banner
        # #2650 remedy: re-register the per-loop `/loop` via `/t3:loops`, or force a
        # render with the PLURAL `t3 loops tick` — never the retired singular
        # `t3 loop tick` fat-loop shim.
        assert "/t3:loops" in banner
        assert "t3 loops tick" in banner
        assert "t3 loop tick" not in banner

    def test_banner_colorized_wraps_red(self) -> None:
        banner = staleness_banner(6 * 3600, colorize=True)
        assert banner.startswith("\033[1;31m")
        assert banner.endswith("\033[0m")


def _write_meta(tmp_path: Path, *, rendered_at: float | None, **extra: object) -> Path:
    statusline = tmp_path / "statusline.txt"
    statusline.write_text("t3-teatree 3m · next tick 4m\n", encoding="utf-8")
    meta: dict[str, object] = dict(extra)
    if rendered_at is not None:
        meta["rendered_at"] = rendered_at
    (tmp_path / "tick-meta.json").write_text(json.dumps(meta) + "\n", encoding="utf-8")
    return statusline


class TestStalenessBannerFor:
    def test_fresh_render_no_banner(self, tmp_path: Path) -> None:
        statusline = _write_meta(tmp_path, rendered_at=1000.0)
        # 30s after render, cadence 720 → cutoff 1440s → fresh
        assert staleness_banner_for(statusline, cadence_seconds=720, now=1030.0) == ""

    def test_stale_render_emits_banner(self, tmp_path: Path) -> None:
        statusline = _write_meta(tmp_path, rendered_at=1000.0)
        # 6h after render → well past the 1440s cutoff
        banner = staleness_banner_for(statusline, cadence_seconds=720, now=1000.0 + 6 * 3600, colorize=False)
        assert "STALE" in banner
        assert "6h ago" in banner

    def test_exactly_at_cutoff_is_fresh(self, tmp_path: Path) -> None:
        statusline = _write_meta(tmp_path, rendered_at=0.0)
        # age == cutoff is NOT stale (strictly greater triggers)
        assert staleness_banner_for(statusline, cadence_seconds=720, now=float(stale_cutoff_seconds(720))) == ""

    def test_just_past_cutoff_is_stale(self, tmp_path: Path) -> None:
        statusline = _write_meta(tmp_path, rendered_at=0.0)
        now = float(stale_cutoff_seconds(720) + 1)
        assert staleness_banner_for(statusline, cadence_seconds=720, now=now, colorize=False) != ""

    def test_missing_rendered_at_fails_open(self, tmp_path: Path) -> None:
        # tick-meta exists (e.g. an old schema with no rendered_at) → fail open, no banner.
        statusline = _write_meta(tmp_path, rendered_at=None, next_epoch=123, cadence=720)
        assert staleness_banner_for(statusline, cadence_seconds=720, now=1e12) == ""

    def test_missing_sidecar_fails_open(self, tmp_path: Path) -> None:
        statusline = tmp_path / "statusline.txt"
        statusline.write_text("content\n", encoding="utf-8")
        assert staleness_banner_for(statusline, cadence_seconds=720, now=1e12) == ""

    def test_broken_sidecar_fails_open(self, tmp_path: Path) -> None:
        statusline = tmp_path / "statusline.txt"
        statusline.write_text("content\n", encoding="utf-8")
        (tmp_path / "tick-meta.json").write_text("{not json", encoding="utf-8")
        assert staleness_banner_for(statusline, cadence_seconds=720, now=1e12) == ""

    def test_non_numeric_rendered_at_fails_open(self, tmp_path: Path) -> None:
        statusline = _write_meta(tmp_path, rendered_at=None)
        (tmp_path / "tick-meta.json").write_text(json.dumps({"rendered_at": "soon"}) + "\n", encoding="utf-8")
        assert staleness_banner_for(statusline, cadence_seconds=720, now=1e12) == ""
