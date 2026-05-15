"""External-overlay sqlite scanner — read tickets from a peer overlay's DB."""

import sqlite3
from pathlib import Path

import pytest

from teatree.loop.scanners.external_tickets import ExternalTicketsScanner


def _build_overlay_db(path: Path, rows: list[tuple[int, str, str, str]]) -> None:
    conn = sqlite3.connect(path)
    try:
        conn.execute(
            "CREATE TABLE teatree_ticket (id INTEGER PRIMARY KEY, state TEXT, issue_url TEXT, overlay TEXT)",
        )
        conn.executemany(
            "INSERT INTO teatree_ticket (id, state, issue_url, overlay) VALUES (?, ?, ?, ?)",
            rows,
        )
        conn.commit()
    finally:
        conn.close()


class TestExternalTicketsScannerScan:
    def test_returns_empty_when_db_missing(self, tmp_path: Path) -> None:
        scanner = ExternalTicketsScanner(overlay_name="foo", db_path=tmp_path / "absent.sqlite3")
        assert scanner.scan() == []

    def test_returns_signals_for_active_tickets(self, tmp_path: Path) -> None:
        db = tmp_path / "db.sqlite3"
        _build_overlay_db(
            db,
            rows=[
                (1, "scoped", "https://example.com/1", "foo"),
                (2, "delivered", "https://example.com/2", "foo"),
                (3, "started", "https://example.com/3", "foo"),
            ],
        )
        signals = ExternalTicketsScanner(overlay_name="foo", db_path=db).scan()
        assert [s.payload["ticket_id"] for s in signals] == [1, 3]
        assert signals[0].kind == "ticket.active"
        assert signals[0].summary == "#1 scoped"
        assert signals[0].payload["issue_url"] == "https://example.com/1"

    def test_ignores_delivered_and_ignored_states(self, tmp_path: Path) -> None:
        db = tmp_path / "db.sqlite3"
        _build_overlay_db(
            db,
            rows=[
                (1, "delivered", "u1", "foo"),
                (2, "ignored", "u2", "foo"),
            ],
        )
        assert ExternalTicketsScanner(overlay_name="foo", db_path=db).scan() == []

    def test_handles_null_issue_url(self, tmp_path: Path) -> None:
        db = tmp_path / "db.sqlite3"
        _build_overlay_db(db, rows=[(1, "scoped", "", "foo")])
        signal = ExternalTicketsScanner(overlay_name="foo", db_path=db).scan()[0]
        assert signal.payload["issue_url"] == ""

    def test_returns_empty_and_logs_on_corrupt_db(self, tmp_path: Path, caplog: pytest.LogCaptureFixture) -> None:
        db = tmp_path / "db.sqlite3"
        db.write_bytes(b"not a sqlite file")
        with caplog.at_level("WARNING"):
            assert ExternalTicketsScanner(overlay_name="foo", db_path=db).scan() == []
        assert any("Cannot read" in record.message for record in caplog.records)

    def test_name_is_set_after_init(self, tmp_path: Path) -> None:
        scanner = ExternalTicketsScanner(overlay_name="foo", db_path=tmp_path / "x")
        assert scanner.name == "external_tickets"
