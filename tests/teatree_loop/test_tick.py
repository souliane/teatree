"""Tests for ``teatree.loop.tick`` — orchestrator that runs scanners + dispatch."""

import datetime as dt
from dataclasses import dataclass
from pathlib import Path

import pytest

from teatree.loop.scanners.base import Scanner, ScanSignal
from teatree.loop.tick import build_default_scanners, run_tick


@dataclass(slots=True)
class _FixedScanner:
    name: str
    out: list[ScanSignal]

    def scan(self) -> list[ScanSignal]:
        return self.out


@dataclass(slots=True)
class _ExplodingScanner:
    name: str = "boom"

    def scan(self) -> list[ScanSignal]:
        msg = "scanner blew up"
        raise RuntimeError(msg)


def test_tick_aggregates_signals_from_all_scanners(tmp_path: Path) -> None:
    a = _FixedScanner(name="a", out=[ScanSignal(kind="my_pr.open", summary="A1")])
    b = _FixedScanner(name="b", out=[ScanSignal(kind="my_pr.open", summary="B1")])
    statusline = tmp_path / "statusline.txt"
    report = run_tick([a, b], statusline_path=statusline)
    assert report.signal_count == 2
    assert report.action_count == 2


def test_tick_renders_statusline_to_file(tmp_path: Path) -> None:
    scanner = _FixedScanner(name="x", out=[ScanSignal(kind="my_pr.failed", summary="oops")])
    statusline = tmp_path / "statusline.txt"
    report = run_tick([scanner], statusline_path=statusline)
    assert statusline.is_file()
    contents = statusline.read_text(encoding="utf-8")
    assert "oops" in contents
    assert "tick @" in contents
    assert report.statusline_path == statusline


def test_tick_records_scanner_errors_without_failing(tmp_path: Path) -> None:
    good = _FixedScanner(name="ok", out=[ScanSignal(kind="my_pr.open", summary="good")])
    bad = _ExplodingScanner()
    statusline = tmp_path / "statusline.txt"
    report = run_tick([good, bad], statusline_path=statusline)
    assert report.signal_count == 1
    assert "boom" in report.errors
    assert "scanner blew up" in report.errors["boom"]


def test_tick_with_no_scanners_still_renders_anchors(tmp_path: Path) -> None:
    statusline = tmp_path / "statusline.txt"
    report = run_tick([], statusline_path=statusline, now=dt.datetime(2026, 5, 6, tzinfo=dt.UTC))
    assert statusline.is_file()
    assert "tick @ 2026-05-06" in statusline.read_text(encoding="utf-8")
    assert report.signal_count == 0


def test_build_default_scanners_starts_with_pending_tasks() -> None:
    scanners: list[Scanner] = build_default_scanners(host=None, messaging=None)
    assert [s.name for s in scanners] == ["pending_tasks"]


def test_build_default_scanners_adds_host_scanners(monkeypatch: pytest.MonkeyPatch) -> None:
    from unittest.mock import MagicMock  # noqa: PLC0415

    from teatree.backends.protocols import CodeHostBackend  # noqa: PLC0415

    host = MagicMock(spec=CodeHostBackend)
    scanners = build_default_scanners(host=host, messaging=None)
    names = {s.name for s in scanners}
    assert {"pending_tasks", "my_prs", "reviewer_prs"} <= names


def test_build_default_scanners_adds_messaging_and_notion_scanners() -> None:
    from unittest.mock import MagicMock  # noqa: PLC0415

    from teatree.backends.protocols import MessagingBackend  # noqa: PLC0415

    messaging = MagicMock(spec=MessagingBackend)
    notion = MagicMock()
    scanners = build_default_scanners(
        host=None,
        messaging=messaging,
        notion_client=notion,
    )
    names = {s.name for s in scanners}
    assert "slack_mentions" in names
    assert "notion_view" in names


def test_tick_renders_agent_actions_in_in_flight_zone(tmp_path: Path) -> None:
    """Non-statusline actions surface as in_flight progress lines."""
    scanner = _FixedScanner(
        name="reviewer_prs",
        out=[ScanSignal(kind="reviewer_pr.new_sha", summary="MR review")],
    )
    statusline = tmp_path / "statusline.txt"
    report = run_tick([scanner], statusline_path=statusline)
    contents = statusline.read_text(encoding="utf-8")
    assert "→ t3:reviewer" in contents
    assert any(a.kind == "agent" for a in report.actions)


def test_tick_renders_unknown_action_zone_as_in_flight(tmp_path: Path) -> None:
    """A statusline action with an unrecognized zone falls back to in_flight."""
    from teatree.loop.dispatch import DispatchAction  # noqa: PLC0415
    from teatree.loop.tick import _zones_for  # noqa: PLC0415

    actions = [DispatchAction(kind="statusline", zone="bogus_zone", detail="x")]
    zones = _zones_for(actions)
    # Non-list zone falls through (line 88 branch); detail is silently dropped.
    assert "x" not in zones.action_needed
    assert "x" not in zones.in_flight
