"""Integration tests: ``run_tick`` caches open PRs and renders the anchor (#271)."""

import json
from dataclasses import dataclass
from pathlib import Path

from teatree.loop.open_prs import read_open_prs_cache
from teatree.loop.scanners.base import ScanSignal
from teatree.loop.tick import TickRequest, run_tick


@dataclass(slots=True)
class _FixedScanner:
    name: str
    out: list[ScanSignal]

    def scan(self) -> list[ScanSignal]:
        return self.out


def _pr_signal(kind: str, *, iid: int, title: str, draft: bool = False) -> ScanSignal:
    return ScanSignal(
        kind=kind,
        summary=f"PR #{iid}: {title}",
        payload={
            "iid": iid,
            "title": title,
            "url": f"https://h/p/{iid}",
            "overlay": "teatree",
            "raw": {"draft": draft},
        },
    )


def test_tick_writes_open_prs_cache_from_scanner_signals(tmp_path: Path) -> None:
    scanner = _FixedScanner(
        name="my_prs",
        out=[
            _pr_signal("my_pr.open", iid=1, title="ready"),
            _pr_signal("my_pr.draft_notes", iid=2, title="wip", draft=True),
        ],
    )
    statusline = tmp_path / "statusline.txt"
    run_tick(TickRequest(scanners=[scanner]), statusline_path=statusline)

    cache = tmp_path / "open-prs.json"
    assert cache.is_file()
    rows = json.loads(cache.read_text(encoding="utf-8"))
    assert {row["iid"] for row in rows} == {1, 2}

    prs = read_open_prs_cache(statusline_path=statusline)
    assert sum(1 for pr in prs if pr.draft) == 1


def test_tick_renders_open_prs_anchor_line(tmp_path: Path) -> None:
    scanner = _FixedScanner(
        name="my_prs",
        out=[
            _pr_signal("my_pr.open", iid=10, title="alpha"),
            _pr_signal("my_pr.open", iid=11, title="beta", draft=True),
        ],
    )
    statusline = tmp_path / "statusline.txt"
    run_tick(TickRequest(scanners=[scanner]), statusline_path=statusline)

    content = statusline.read_text(encoding="utf-8")
    assert "open PRs: 2 (1 draft)" in content
    assert "#10 alpha" in content
    assert "#11 [draft] beta" in content


def test_tick_with_no_open_prs_writes_empty_cache_and_no_anchor(tmp_path: Path) -> None:
    scanner = _FixedScanner(name="my_prs", out=[ScanSignal(kind="ticket.stale", summary="not a pr")])
    statusline = tmp_path / "statusline.txt"
    run_tick(TickRequest(scanners=[scanner]), statusline_path=statusline)

    assert read_open_prs_cache(statusline_path=statusline) == []
    assert "open PRs:" not in statusline.read_text(encoding="utf-8")


def test_tick_with_no_scanners_clears_stale_cache(tmp_path: Path) -> None:
    statusline = tmp_path / "statusline.txt"
    (tmp_path / "open-prs.json").write_text(
        json.dumps([{"iid": 99, "title": "stale", "url": "https://h/p/99", "overlay": "teatree", "draft": False}]),
        encoding="utf-8",
    )
    run_tick(TickRequest(scanners=[]), statusline_path=statusline)
    assert read_open_prs_cache(statusline_path=statusline) == []
