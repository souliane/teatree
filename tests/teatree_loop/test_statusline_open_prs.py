"""Tests for the statusline open-PR anchor (#271).

Renders the anchor against a stubbed PR set written to a real cache file
under ``tmp_path`` — no mocking of the reader, the on-disk sidecar is the
contract the tick and the anchor share.
"""

from pathlib import Path
from unittest.mock import patch

from teatree.loop.open_prs import OpenPr, open_prs_anchor, write_open_prs_cache
from teatree.loop.statusline import StatuslineEntry, StatuslineZones, render


def _stub_cache(statusline: Path, prs: list[OpenPr]) -> None:
    write_open_prs_cache(prs, statusline_path=statusline)


class TestOpenPrsAnchor:
    def test_headline_counts_all_open_prs(self, tmp_path: Path) -> None:
        statusline = tmp_path / "statusline.txt"
        _stub_cache(
            statusline,
            [
                OpenPr(iid=1, title="a", url="https://h/p/1", overlay="teatree", draft=False),
                OpenPr(iid=2, title="b", url="https://h/p/2", overlay="teatree", draft=False),
                OpenPr(iid=3, title="c", url="https://h/p/3", overlay="teatree", draft=False),
            ],
        )
        rows = open_prs_anchor(target=statusline)
        assert rows[0] == "open PRs: 3"

    def test_headline_breaks_out_draft_count(self, tmp_path: Path) -> None:
        statusline = tmp_path / "statusline.txt"
        _stub_cache(
            statusline,
            [
                OpenPr(iid=1, title="ready", url="https://h/p/1", overlay="teatree", draft=False),
                OpenPr(iid=2, title="wip", url="https://h/p/2", overlay="teatree", draft=True),
            ],
        )
        assert open_prs_anchor(target=statusline)[0] == "open PRs: 2 (1 draft)"

    def test_lists_each_pr_as_a_clickable_entry(self, tmp_path: Path) -> None:
        statusline = tmp_path / "statusline.txt"
        _stub_cache(
            statusline,
            [OpenPr(iid=42, title="my title", url="https://h/p/42", overlay="teatree", draft=False)],
        )
        rows = open_prs_anchor(target=statusline)
        entry = rows[1]
        assert isinstance(entry, StatuslineEntry)
        assert entry.text == "  #42 my title"
        assert entry.url == "https://h/p/42"

    def test_marks_draft_prs_in_the_list(self, tmp_path: Path) -> None:
        statusline = tmp_path / "statusline.txt"
        _stub_cache(
            statusline,
            [OpenPr(iid=9, title="wip", url="https://h/p/9", overlay="teatree", draft=True)],
        )
        entry = open_prs_anchor(target=statusline)[1]
        assert isinstance(entry, StatuslineEntry)
        assert entry.text == "  #9 [draft] wip"

    def test_caps_the_list_and_appends_overflow(self, tmp_path: Path) -> None:
        statusline = tmp_path / "statusline.txt"
        _stub_cache(
            statusline,
            [OpenPr(iid=i, title=f"t{i}", url=f"https://h/p/{i}", overlay="teatree", draft=False) for i in range(7)],
        )
        rows = open_prs_anchor(target=statusline)
        assert rows[0] == "open PRs: 7"
        assert len(rows) == 1 + 5 + 1
        assert rows[-1] == "  (+2 more)"

    def test_no_cache_renders_no_line(self, tmp_path: Path) -> None:
        assert open_prs_anchor(target=tmp_path / "statusline.txt") == []

    def test_empty_snapshot_renders_no_line(self, tmp_path: Path) -> None:
        statusline = tmp_path / "statusline.txt"
        _stub_cache(statusline, [])
        assert open_prs_anchor(target=statusline) == []

    def test_read_error_fails_open_to_no_line(self, tmp_path: Path) -> None:
        statusline = tmp_path / "statusline.txt"
        _stub_cache(statusline, [OpenPr(iid=1, title="x", url="https://h/p/1", overlay="teatree", draft=False)])
        with patch("teatree.loop.open_prs._read_open_prs", side_effect=RuntimeError("boom")):
            assert open_prs_anchor(target=statusline) == []

    def test_anchor_flows_into_rendered_statusline_file(self, tmp_path: Path) -> None:
        statusline = tmp_path / "statusline.txt"
        _stub_cache(
            statusline,
            [OpenPr(iid=2168, title="some change", url="https://h/p/2168", overlay="teatree", draft=False)],
        )
        zones = StatuslineZones(anchors=open_prs_anchor(target=statusline))
        render(zones, target=statusline, colorize=False)
        content = statusline.read_text(encoding="utf-8")
        assert "open PRs: 1" in content
        assert "#2168 some change" in content
