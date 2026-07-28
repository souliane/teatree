"""Reclaiming a pruned control DB's free pages back to the filesystem (#3852).

A retention prune deletes rows; SQLite moves their pages onto the free list and
leaves the FILE exactly as large. Every test here is paired with the control that
proves the premise — that a delete alone reclaims nothing — so a green cannot come
from the fixture never having been bloated in the first place.
"""

import sqlite3
from pathlib import Path
from unittest.mock import patch

from django.db import DEFAULT_DB_ALIAS, connections

from teatree.utils.django_db.vacuum import vacuum_control_db, vacuum_sqlite

_ROWS = 4000
_PAYLOAD = "x" * 512
_MODULE = "teatree.utils.django_db.vacuum"


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


class _StubConnection:
    """A Django connection stand-in — enough surface for the resolver, no real DB.

    The file case must NOT be driven through the suite's own default connection:
    the vacuum closes it, and an ``:memory:`` database does not survive its
    connection, so doing so would drop the test database out from under every
    later test in the worker.
    """

    def __init__(self, name: Path | str, *, vendor: str = "sqlite") -> None:
        self.vendor = vendor
        self.settings_dict = {"NAME": str(name)}
        self.closed = False

    def close(self) -> None:
        self.closed = True


class TestVacuumControlDb:
    """Resolving the control DB behind a Django connection."""

    def test_the_suites_in_memory_connection_is_a_stated_no_op(self) -> None:
        """Against the REAL default connection — a vacuum must never break a test run."""
        outcome = vacuum_control_db()

        assert not outcome.ran
        assert "in-memory" in outcome.reason
        assert connections[DEFAULT_DB_ALIAS].connection is not None, "the in-memory test DB was closed"

    def test_a_non_sqlite_vendor_is_refused_rather_than_given_a_sqlite_shaped_step(self) -> None:
        stub = _StubConnection("/anywhere/db.sqlite3", vendor="postgresql")

        with patch(f"{_MODULE}.connections", {DEFAULT_DB_ALIAS: stub}):
            outcome = vacuum_control_db()

        assert not outcome.ran
        assert "not SQLite" in outcome.reason
        assert not stub.closed

    def test_it_closes_the_connection_then_vacuums_the_file_it_names(self, tmp_path: Path) -> None:
        db = tmp_path / "control.sqlite3"
        _bloated_db(db)
        _delete_most_rows(db)
        before = db.stat().st_size
        stub = _StubConnection(db)

        with patch(f"{_MODULE}.connections", {DEFAULT_DB_ALIAS: stub}):
            outcome = vacuum_control_db()

        assert stub.closed, "the rebuild must not contend with a connection holding the file open"
        assert outcome.ran
        assert db.stat().st_size < before
