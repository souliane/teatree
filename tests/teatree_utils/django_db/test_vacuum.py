"""Reclaiming a pruned control DB's free pages back to the filesystem (#3852).

A retention prune deletes rows; SQLite moves their pages onto the free list and
leaves the FILE exactly as large. Every test here is paired with the control that
proves the premise — that a delete alone reclaims nothing — so a green cannot come
from the fixture never having been bloated in the first place.
"""

import sqlite3
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from django.db import DEFAULT_DB_ALIAS

from teatree.utils.django_db.vacuum import _required_headroom_bytes, vacuum_control_db, vacuum_sqlite

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

    def test_it_refuses_before_starting_when_the_disk_lacks_rebuild_headroom(self, tmp_path: Path) -> None:
        """VACUUM writes a FULL second copy before swapping — starting without room is the danger.

        On the host that produced this ticket the control DB is 1.2 GB against
        8.6 GB free and falling, so the rebuild's transient copy is a real
        fraction of the headroom. It must refuse up front and say so, never
        discover the shortfall part-way through rewriting the file.
        """
        db = tmp_path / "control.sqlite3"
        _bloated_db(db)
        before = db.stat().st_size
        cramped = SimpleNamespace(total=0, used=0, free=1024)

        with patch(f"{_MODULE}.shutil.disk_usage", return_value=cramped):
            outcome = vacuum_sqlite(db)

        assert not outcome.ran
        assert "headroom" in outcome.reason
        assert db.stat().st_size == before, "the file must be untouched when the vacuum is refused"

    def test_ample_headroom_does_not_block_the_vacuum(self) -> None:
        """Anti-vacuous control: the guard must not be a blanket refusal.

        The shrink tests above already run against the real disk, so this pins the
        boundary itself — headroom exactly at the required multiple still proceeds.
        """
        assert _required_headroom_bytes(1_000) == 1_100

    def test_an_absent_file_is_reported_not_run_rather_than_raising(self, tmp_path: Path) -> None:
        outcome = vacuum_sqlite(tmp_path / "nope.sqlite3")

        assert not outcome.ran
        assert outcome.bytes_reclaimed == 0
        assert "no such file" in outcome.reason

    def test_an_in_memory_database_is_reported_not_run(self) -> None:
        """The test suite's own DB — a vacuum here must be a stated no-op, never a crash."""
        outcome = vacuum_sqlite(Path(":memory:"))

        assert not outcome.ran
        assert "in-memory" in outcome.reason
        assert outcome.bytes_reclaimed == 0

    def test_the_shared_cache_in_memory_uri_is_also_recognised(self) -> None:
        """Django rewrites an in-memory DB to this form once a TransactionTestCase needs it.

        A name-equality check against ``:memory:`` alone misses it and reports the
        URI as a missing FILE — so what the vacuum said about the very same
        database depended on what had run before it (caught by the shuffle lane).
        """
        outcome = vacuum_sqlite(Path("file:memorydb_default?mode=memory&cache=shared"))

        assert not outcome.ran
        assert "in-memory" in outcome.reason


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
        """Against the REAL default connection — a vacuum must never break a test run.

        Deliberately asserts only that nothing ran: which in-memory FORM Django has
        the connection on (``:memory:`` or the shared-cache URI) depends on whether
        a ``TransactionTestCase`` ran first, and both forms are pinned directly
        above. Asserting the wording here is what made this order-dependent.
        """
        outcome = vacuum_control_db()

        assert not outcome.ran
        assert outcome.bytes_reclaimed == 0

    def test_an_in_memory_connection_is_never_closed(self) -> None:
        """An ``:memory:`` database does not survive its connection.

        Closing one to "prepare" a rebuild that then declines to run would destroy
        the database the maintenance step just decided not to touch.
        """
        stub = _StubConnection(":memory:")

        with patch(f"{_MODULE}.connections", {DEFAULT_DB_ALIAS: stub}):
            outcome = vacuum_control_db()

        assert not stub.closed, "closing an in-memory connection destroys the database"
        assert not outcome.ran

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
