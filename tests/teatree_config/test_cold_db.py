"""Integration tests for the Django-free stdlib sqlite plumbing (`cold_db`).

The raw read layer under `cold_reader`: config-DB path resolution, the loop-state
status read, and the generic existence probe. The happy paths build a REAL sqlite
database via stdlib `sqlite3` and read it back — the fail-open, WAL-fallback, and
locking behaviour exercised against actual sqlite. The rarer fail-open branches
(a PRAGMA-setup failure, the exact `SQLITE_CANTOPEN` quiescent-WAL retry, a
non-`OperationalError` sqlite error) are driven with a fake connection so each
error class is deterministic rather than OS-dependent.
"""

import json
import sqlite3
from collections.abc import Iterable
from pathlib import Path

import pytest

import teatree.paths
from teatree.config import cold_db
from teatree.config.cold_db import canonical_config_db, fetch_one, loop_status, row_exists

_PRAGMA_FAIL = "pragma failed"


class _CantOpen(sqlite3.OperationalError):
    """An OperationalError that reports the quiescent-WAL SQLITE_CANTOPEN code."""

    sqlite_errorcode = sqlite3.SQLITE_CANTOPEN


class _FakeConn:
    """A stand-in sqlite connection driving the fail-open branches deterministically.

    PRAGMA setup may fail, and the query execute either returns a row or raises a
    chosen error — so each error class is exercised without OS-dependent WAL timing.
    """

    def __init__(self, *, pragma_error: bool = False, exec_error: Exception | None = None) -> None:
        self.pragma_error = pragma_error
        self.exec_error = exec_error
        self.closed = False

    def execute(self, sql: str, *_args: object) -> "_FakeConn":
        if sql.startswith("PRAGMA"):
            if self.pragma_error:
                raise sqlite3.OperationalError(_PRAGMA_FAIL)
            return self
        if self.exec_error is not None:
            raise self.exec_error
        return self

    def fetchone(self) -> tuple[object, ...]:
        return ("row",)

    def close(self) -> None:
        self.closed = True


def _make_config_db(path: Path, rows: Iterable[tuple[str, str, object]], *, wal: bool = False) -> None:
    """Build a real `teatree_config_setting` DB matching the Django migration."""
    conn = sqlite3.connect(path)
    try:
        if wal:
            conn.execute("PRAGMA journal_mode=WAL")
        conn.execute(
            "CREATE TABLE teatree_config_setting ("
            "id INTEGER PRIMARY KEY, scope TEXT NOT NULL DEFAULT '', "
            "key TEXT NOT NULL, value TEXT NOT NULL)"
        )
        conn.executemany(
            "INSERT INTO teatree_config_setting (scope, key, value) VALUES (?, ?, ?)",
            [(scope, key, json.dumps(value)) for scope, key, value in rows],
        )
        conn.commit()
    finally:
        conn.close()


def _make_loop_state_db(path: Path, rows: Iterable[tuple[str, str]], *, wal: bool = False) -> None:
    """Build a real `teatree_loop_state` DB matching the Django migration."""
    conn = sqlite3.connect(path)
    try:
        if wal:
            conn.execute("PRAGMA journal_mode=WAL")
        conn.execute(
            "CREATE TABLE teatree_loop_state ("
            "id INTEGER PRIMARY KEY, name TEXT NOT NULL UNIQUE, "
            "status TEXT NOT NULL, created_at TEXT, updated_at TEXT)"
        )
        conn.executemany("INSERT INTO teatree_loop_state (name, status) VALUES (?, ?)", list(rows))
        conn.commit()
    finally:
        conn.close()


def _remove_wal_sidecars(db: Path) -> None:
    """Delete the ``-wal``/``-shm`` sidecar files of `db` (the quiescent cold state)."""
    for suffix in ("-wal", "-shm"):
        db.with_name(db.name + suffix).unlink(missing_ok=True)


class TestCanonicalConfigDb:
    def test_t3_config_db_override_wins(self, tmp_path: Path) -> None:
        override = tmp_path / "explicit.sqlite3"
        env = {"T3_CONFIG_DB": str(override), "XDG_DATA_HOME": str(tmp_path / "ignored")}
        assert canonical_config_db(env=env, home=tmp_path) == override

    def test_xdg_data_home_is_honored(self, tmp_path: Path) -> None:
        xdg = tmp_path / "xdg"
        env = {"XDG_DATA_HOME": str(xdg)}
        assert canonical_config_db(env=env, home=tmp_path) == xdg / "teatree" / "db.sqlite3"

    def test_default_is_local_share(self, tmp_path: Path) -> None:
        resolved = canonical_config_db(env={}, home=tmp_path)
        assert resolved == tmp_path / ".local" / "share" / "teatree" / "db.sqlite3"

    def test_pinned_equal_to_paths_true_canonical_db(self) -> None:
        # The duplicated path computation must never drift from teatree.paths.
        # paths.py froze TRUE_CANONICAL_DB from Path.home() at import; the test
        # harness rebinds Path.home() per-test, so pin against the home it used
        # (home/.local/share/teatree/db.sqlite3 → parents[3] is that home).
        paths_home = teatree.paths.TRUE_CANONICAL_DB.parents[3]
        assert canonical_config_db(env={}, home=paths_home) == teatree.paths.TRUE_CANONICAL_DB

    def test_worktree_cwd_does_not_isolate(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        worktree = tmp_path / "wt"
        worktree.mkdir()
        (worktree / ".git").write_text("gitdir: /some/main/.git/worktrees/wt\n")
        monkeypatch.chdir(worktree)

        primary = canonical_config_db(env={}, home=tmp_path)
        assert primary == tmp_path / ".local" / "share" / "teatree" / "db.sqlite3"

        # Anti-vacuity: the same worktree resolved through teatree.paths DOES
        # isolate onto a sibling DB — proving the cold reader's deliberate inverse.
        isolated = teatree.paths.resolve_data_dir(env={}, home=tmp_path, repo_root=worktree)
        assert isolated.auto_isolated is True
        assert isolated.path / "db.sqlite3" != primary


class TestLoopStatus:
    """`loop_status` is the Django-free cold twin of `LoopState.objects.status_of`."""

    def test_reads_seeded_status(self, tmp_path: Path) -> None:
        db = tmp_path / "db.sqlite3"
        _make_loop_state_db(db, [("dispatch", "paused"), ("review", "disabled")])
        assert loop_status("dispatch", db_path=db) == "paused"
        assert loop_status("review", db_path=db) == "disabled"

    def test_absent_row_returns_enabled_default(self, tmp_path: Path) -> None:
        # The manager's absent-row fall-through: an empty table means every loop
        # runs. Anti-vacuous: default="enabled" differs from a would-be None.
        db = tmp_path / "db.sqlite3"
        _make_loop_state_db(db, [("review", "paused")])
        assert loop_status("dispatch", db_path=db) == "enabled"

    def test_custom_default_honoured_on_absent_row(self, tmp_path: Path) -> None:
        db = tmp_path / "db.sqlite3"
        _make_loop_state_db(db, [])
        assert loop_status("dispatch", default="sentinel", db_path=db) == "sentinel"

    def test_missing_db_fails_open_to_default(self, tmp_path: Path) -> None:
        assert loop_status("dispatch", db_path=tmp_path / "nope.sqlite3") == "enabled"

    def test_missing_table_fails_open_to_default(self, tmp_path: Path) -> None:
        db = tmp_path / "fresh.sqlite3"
        sqlite3.connect(db).close()  # exists but has no teatree_loop_state table
        assert loop_status("dispatch", db_path=db) == "enabled"

    def test_reads_via_t3_config_db_env(self, tmp_path: Path) -> None:
        db = tmp_path / "db.sqlite3"
        _make_loop_state_db(db, [("dispatch", "disabled")])
        assert loop_status("dispatch", env={"T3_CONFIG_DB": str(db)}) == "disabled"

    def test_quiescent_wal_db_readable(self, tmp_path: Path) -> None:
        # The realistic cold state: a WAL-format DB with no live writer and no
        # sidecars. The shared `fetch_one` immutable=1 fallback reads it.
        db = tmp_path / "wal.sqlite3"
        _make_loop_state_db(db, [("dispatch", "paused")], wal=True)
        _remove_wal_sidecars(db)
        assert not db.with_name(db.name + "-wal").exists()
        assert loop_status("dispatch", db_path=db) == "paused"


class TestRowExists:
    """`row_exists` is the Django-free existence probe backing the UPS fast path."""

    def test_true_when_a_row_matches(self, tmp_path: Path) -> None:
        db = tmp_path / "db.sqlite3"
        _make_config_db(db, [("", "mode", "auto")])
        q = "SELECT 1 FROM teatree_config_setting WHERE key=? LIMIT 1"
        assert row_exists(q, ("mode",), on_error=True, db_path=db) is True

    def test_false_when_query_runs_but_matches_nothing(self, tmp_path: Path) -> None:
        # Confirmed empty → False regardless of on_error (anti-vacuous: on_error=True).
        db = tmp_path / "db.sqlite3"
        _make_config_db(db, [("", "mode", "auto")])
        q = "SELECT 1 FROM teatree_config_setting WHERE key=? LIMIT 1"
        assert row_exists(q, ("absent",), on_error=True, db_path=db) is False

    def test_missing_db_returns_on_error(self, tmp_path: Path) -> None:
        q = "SELECT 1 FROM teatree_config_setting LIMIT 1"
        missing = tmp_path / "nope.sqlite3"
        assert row_exists(q, on_error=True, db_path=missing) is True
        assert row_exists(q, on_error=False, db_path=missing) is False

    def test_missing_table_returns_on_error(self, tmp_path: Path) -> None:
        db = tmp_path / "fresh.sqlite3"
        sqlite3.connect(db).close()  # exists, but no such table
        q = "SELECT 1 FROM teatree_deferred_question LIMIT 1"
        assert row_exists(q, on_error=True, db_path=db) is True
        assert row_exists(q, on_error=False, db_path=db) is False

    def test_locked_db_returns_on_error(self, tmp_path: Path) -> None:
        db = tmp_path / "db.sqlite3"
        _make_config_db(db, [("", "mode", "auto")])
        writer = sqlite3.connect(db)
        writer.isolation_level = None
        writer.execute("BEGIN EXCLUSIVE")  # blocks the RO reader's SHARED lock
        try:
            q = "SELECT 1 FROM teatree_config_setting LIMIT 1"
            assert row_exists(q, on_error=True, db_path=db) is True
            assert row_exists(q, on_error=False, db_path=db) is False
        finally:
            writer.rollback()
            writer.close()

    def test_quiescent_wal_db_confirms_cleanly(self, tmp_path: Path) -> None:
        db = tmp_path / "wal.sqlite3"
        _make_config_db(db, [("", "mode", "auto")], wal=True)
        _remove_wal_sidecars(db)
        q = "SELECT 1 FROM teatree_config_setting WHERE key=? LIMIT 1"
        assert row_exists(q, ("mode",), on_error=False, db_path=db) is True
        assert row_exists(q, ("absent",), on_error=True, db_path=db) is False


class TestFetchOne:
    """`fetch_one` is the shared single-row read; the sentinel collapses to None."""

    def test_reads_the_row(self, tmp_path: Path) -> None:
        db = tmp_path / "db.sqlite3"
        _make_config_db(db, [("", "mode", "auto")])
        row = fetch_one(db, "SELECT value FROM teatree_config_setting WHERE key=?", ("mode",))
        assert row == ('"auto"',)

    def test_missing_row_is_none(self, tmp_path: Path) -> None:
        db = tmp_path / "db.sqlite3"
        _make_config_db(db, [("", "mode", "auto")])
        assert fetch_one(db, "SELECT value FROM teatree_config_setting WHERE key=?", ("absent",)) is None

    def test_error_collapses_to_none(self, tmp_path: Path) -> None:
        db = tmp_path / "fresh.sqlite3"
        sqlite3.connect(db).close()  # exists, but the table is absent → sentinel → None
        assert fetch_one(db, "SELECT value FROM teatree_config_setting WHERE key=?", ("mode",)) is None


class TestReadOnlyFailOpenBranches:
    """The sqlite fail-open branches of the read path.

    Driven with a fake conn so each error class is deterministic (no OS-dependent WAL timing).
    """

    def test_pragma_setup_failure_closes_and_fails_open(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        db = tmp_path / "db.sqlite3"
        db.touch()
        conn = _FakeConn(pragma_error=True)
        monkeypatch.setattr(cold_db.sqlite3, "connect", lambda *_a, **_k: conn)
        assert row_exists("SELECT 1", on_error=True, db_path=db) is True
        assert conn.closed is True  # the failed open never strands the handle

    def test_non_operational_sqlite_error_fails_open(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        db = tmp_path / "db.sqlite3"
        db.touch()
        conn = _FakeConn(exec_error=sqlite3.IntegrityError("corrupt"))
        monkeypatch.setattr(cold_db.sqlite3, "connect", lambda *_a, **_k: conn)
        assert row_exists("SELECT 1", on_error=True, db_path=db) is True

    def test_mode_ro_cantopen_retries_immutable_and_reads(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        db = tmp_path / "db.sqlite3"
        db.touch()
        conns = [_FakeConn(exec_error=_CantOpen("quiescent")), _FakeConn()]  # mode=ro CANTOPEN, then immutable OK

        def _connect(conn_str: str, *_a: object, **_k: object) -> _FakeConn:
            return conns[0] if "mode=ro" in conn_str else conns[1]

        monkeypatch.setattr(cold_db.sqlite3, "connect", _connect)
        assert row_exists("SELECT 1", on_error=False, db_path=db) is True

    def test_both_opens_cantopen_fails_open(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        db = tmp_path / "db.sqlite3"
        db.touch()
        monkeypatch.setattr(cold_db.sqlite3, "connect", lambda *_a, **_k: _FakeConn(exec_error=_CantOpen("quiescent")))
        assert row_exists("SELECT 1", on_error=True, db_path=db) is True
