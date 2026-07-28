"""Reclaiming a pruned control DB's free pages back to the filesystem (#3852).

A retention prune deletes rows; SQLite moves their pages onto the free list and
leaves the FILE exactly as large. Every test here is paired with the control that
proves the premise — that a delete alone reclaims nothing — so a green cannot come
from the fixture never having been bloated in the first place.
"""

import sqlite3
from pathlib import Path

from teatree.utils.django_db.vacuum import vacuum_sqlite

_ROWS = 4000
_PAYLOAD = "x" * 512


def _bloated_db(path: Path) -> None:
    con = sqlite3.connect(path)
    with con:
        con.execute("CREATE TABLE attempt (id INTEGER PRIMARY KEY, blob TEXT)")
        con.executemany("INSERT INTO attempt (blob) VALUES (?)", [(_PAYLOAD,) for _ in range(_ROWS)])
    con.close()


def _delete_most_rows(path: Path) -> None:
    con = sqlite3.connect(path)
    with con:
        con.execute("DELETE FROM attempt WHERE id > 10")
    con.close()


def _free_pages(path: Path) -> int:
    con = sqlite3.connect(path)
    try:
        return int(con.execute("PRAGMA freelist_count").fetchone()[0])
    finally:
        con.close()


class TestVacuumSqlite:
    def test_control_a_delete_alone_leaves_the_file_size_untouched(self, tmp_path: Path) -> None:
        """The premise this whole item rests on — deleted rows become free pages, not free disk."""
        db = tmp_path / "control.sqlite3"
        _bloated_db(db)
        before = db.stat().st_size

        _delete_most_rows(db)

        assert db.stat().st_size == before
        assert _free_pages(db) > 0, "fixture never bloated — the shrink test below would be vacuous"

    def test_vacuum_returns_the_freed_pages_to_the_filesystem(self, tmp_path: Path) -> None:
        db = tmp_path / "control.sqlite3"
        _bloated_db(db)
        _delete_most_rows(db)
        before = db.stat().st_size

        outcome = vacuum_sqlite(db)

        assert outcome.ran
        assert db.stat().st_size < before
        assert outcome.bytes_reclaimed == before - db.stat().st_size
        assert _free_pages(db) == 0

    def test_the_surviving_rows_are_still_readable(self, tmp_path: Path) -> None:
        """VACUUM rebuilds the file — the guard that it rebuilt it correctly."""
        db = tmp_path / "control.sqlite3"
        _bloated_db(db)
        _delete_most_rows(db)

        vacuum_sqlite(db)

        con = sqlite3.connect(db)
        try:
            assert con.execute("SELECT count(*) FROM attempt").fetchone()[0] == 10
        finally:
            con.close()

    def test_an_absent_file_is_reported_not_run_rather_than_raising(self, tmp_path: Path) -> None:
        outcome = vacuum_sqlite(tmp_path / "nope.sqlite3")

        assert not outcome.ran
        assert outcome.bytes_reclaimed == 0
        assert "no such file" in outcome.reason

    def test_an_in_memory_database_is_reported_not_run(self) -> None:
        """The test suite's own DB — a vacuum here must be a stated no-op, never a crash."""
        outcome = vacuum_sqlite(Path(":memory:"))

        assert not outcome.ran
        assert outcome.bytes_reclaimed == 0
