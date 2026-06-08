"""Tests for ``teatree.loop.open_prs`` — the open-PR snapshot cache (#271)."""

from pathlib import Path

from teatree.loop.open_prs import OpenPr, cache_path, open_prs_from_signals, read_open_prs_cache, write_open_prs_cache
from teatree.loop.scanners.base import ScanSignal


def _signal(kind: str, *, iid: int, title: str, draft: bool = False) -> ScanSignal:
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


class TestOpenPrsFromSignals:
    def test_unions_every_open_pr_signal_kind(self) -> None:
        signals = [
            _signal("my_pr.open", iid=1, title="open one"),
            _signal("my_pr.draft_notes", iid=2, title="has notes"),
            _signal("my_pr.failed", iid=3, title="red ci"),
        ]
        prs = open_prs_from_signals(signals)
        assert [pr.iid for pr in prs] == [1, 2, 3]

    def test_ignores_non_pr_signals(self) -> None:
        signals = [
            _signal("my_pr.open", iid=1, title="keep"),
            ScanSignal(kind="ticket.stale", summary="drop me", payload={"iid": 99}),
        ]
        assert [pr.iid for pr in open_prs_from_signals(signals)] == [1]

    def test_reads_draft_flag_from_raw_payload(self) -> None:
        signals = [_signal("my_pr.open", iid=7, title="wip", draft=True)]
        assert open_prs_from_signals(signals)[0].draft is True

    def test_reads_legacy_work_in_progress_alias(self) -> None:
        signal = ScanSignal(
            kind="my_pr.open",
            summary="legacy wip",
            payload={"iid": 8, "title": "old gitlab", "url": "https://h/p/8", "raw": {"work_in_progress": True}},
        )
        assert open_prs_from_signals([signal])[0].draft is True

    def test_dedups_by_url(self) -> None:
        signals = [
            _signal("my_pr.open", iid=5, title="once"),
            _signal("my_pr.failed", iid=5, title="once"),
        ]
        assert len(open_prs_from_signals(signals)) == 1

    def test_missing_draft_field_defaults_to_not_draft(self) -> None:
        signal = ScanSignal(
            kind="my_pr.open",
            summary="no draft key",
            payload={"iid": 6, "title": "ready", "url": "https://h/p/6", "raw": {}},
        )
        assert open_prs_from_signals([signal])[0].draft is False

    def test_keeps_pr_without_a_url(self) -> None:
        signal = ScanSignal(kind="my_pr.open", summary="urlless", payload={"iid": 4, "title": "no url"})
        prs = open_prs_from_signals([signal])
        assert [(pr.iid, pr.url) for pr in prs] == [(4, "")]


class TestOpenPrsCacheRoundTrip:
    def test_write_then_read_recovers_rows(self, tmp_path: Path) -> None:
        statusline = tmp_path / "statusline.txt"
        prs = [
            OpenPr(iid=1, title="alpha", url="https://h/p/1", overlay="teatree", draft=False),
            OpenPr(iid=2, title="beta", url="https://h/p/2", overlay="teatree", draft=True),
        ]
        write_open_prs_cache(prs, statusline_path=statusline)
        assert read_open_prs_cache(statusline_path=statusline) == prs

    def test_cache_lives_next_to_statusline(self, tmp_path: Path) -> None:
        statusline = tmp_path / "statusline.txt"
        assert cache_path(statusline) == tmp_path / "open-prs.json"

    def test_write_creates_parent_directory(self, tmp_path: Path) -> None:
        statusline = tmp_path / "deep" / "nested" / "statusline.txt"
        write_open_prs_cache([], statusline_path=statusline)
        assert (statusline.parent / "open-prs.json").is_file()

    def test_read_missing_cache_returns_empty(self, tmp_path: Path) -> None:
        assert read_open_prs_cache(statusline_path=tmp_path / "statusline.txt") == []

    def test_read_corrupt_cache_returns_empty(self, tmp_path: Path) -> None:
        statusline = tmp_path / "statusline.txt"
        cache_path(statusline).write_text("{not json", encoding="utf-8")
        assert read_open_prs_cache(statusline_path=statusline) == []

    def test_read_non_list_payload_returns_empty(self, tmp_path: Path) -> None:
        statusline = tmp_path / "statusline.txt"
        cache_path(statusline).write_text('{"iid": 1}', encoding="utf-8")
        assert read_open_prs_cache(statusline_path=statusline) == []
