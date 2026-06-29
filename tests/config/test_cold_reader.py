"""Integration tests for the Django-free stdlib cold reader (config-unify PR1).

Every test builds a REAL sqlite database with the `teatree_config_setting`
schema via stdlib `sqlite3` (Django's JSONField stores each value as
JSON-encoded text), then reads it back through `cold_reader` — no mocks, so the
fail-open and locking behaviour is exercised against actual sqlite, not a stub.
"""

import json
import sqlite3
from collections.abc import Iterable
from pathlib import Path

import pytest

import teatree.paths
from teatree.config import cold_reader

Row = tuple[str, str, object]


def _make_db(path: Path, rows: Iterable[Row], *, wal: bool = False) -> None:
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


def _remove_wal_sidecars(db: Path) -> None:
    """Delete the ``-wal``/``-shm`` companions of `db`.

    The realistic quiescent cold state (no teatree process holding the DB),
    where a WAL-format file has no live sidecars.
    """
    for suffix in ("-wal", "-shm"):
        db.with_name(db.name + suffix).unlink(missing_ok=True)


class TestCanonicalConfigDb:
    def test_t3_config_db_override_wins(self, tmp_path: Path) -> None:
        override = tmp_path / "explicit.sqlite3"
        env = {"T3_CONFIG_DB": str(override), "XDG_DATA_HOME": str(tmp_path / "ignored")}
        assert cold_reader.canonical_config_db(env=env, home=tmp_path) == override

    def test_xdg_data_home_is_honored(self, tmp_path: Path) -> None:
        xdg = tmp_path / "xdg"
        env = {"XDG_DATA_HOME": str(xdg)}
        assert cold_reader.canonical_config_db(env=env, home=tmp_path) == xdg / "teatree" / "db.sqlite3"

    def test_default_is_local_share(self, tmp_path: Path) -> None:
        resolved = cold_reader.canonical_config_db(env={}, home=tmp_path)
        assert resolved == tmp_path / ".local" / "share" / "teatree" / "db.sqlite3"

    def test_pinned_equal_to_paths_true_canonical_db(self) -> None:
        # The duplicated path computation must never drift from teatree.paths.
        # paths.py froze TRUE_CANONICAL_DB from Path.home() at import; the test
        # harness rebinds Path.home() per-test, so pin against the home it used
        # (home/.local/share/teatree/db.sqlite3 → parents[3] is that home).
        paths_home = teatree.paths.TRUE_CANONICAL_DB.parents[3]
        assert cold_reader.canonical_config_db(env={}, home=paths_home) == teatree.paths.TRUE_CANONICAL_DB

    def test_worktree_cwd_does_not_isolate(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        worktree = tmp_path / "wt"
        worktree.mkdir()
        (worktree / ".git").write_text("gitdir: /some/main/.git/worktrees/wt\n")
        monkeypatch.chdir(worktree)

        primary = cold_reader.canonical_config_db(env={}, home=tmp_path)
        assert primary == tmp_path / ".local" / "share" / "teatree" / "db.sqlite3"

        # Anti-vacuity: the same worktree resolved through teatree.paths DOES
        # isolate onto a sibling DB — proving the cold reader's deliberate inverse.
        isolated = teatree.paths.resolve_data_dir(env={}, home=tmp_path, repo_root=worktree)
        assert isolated.auto_isolated is True
        assert isolated.path / "db.sqlite3" != primary


class TestReadSettingFailsOpen:
    def test_missing_db_file_returns_none(self, tmp_path: Path) -> None:
        assert cold_reader.read_setting("mode", db_path=tmp_path / "nope.sqlite3") is None

    def test_missing_table_returns_none(self, tmp_path: Path) -> None:
        db = tmp_path / "fresh.sqlite3"
        sqlite3.connect(db).close()  # exists but has no teatree_config_setting table
        assert cold_reader.read_setting("mode", db_path=db) is None

    def test_missing_row_returns_none(self, tmp_path: Path) -> None:
        db = tmp_path / "db.sqlite3"
        _make_db(db, [("", "mode", "auto")])
        assert cold_reader.read_setting("absent_key", db_path=db) is None

    def test_invalid_json_returns_none(self, tmp_path: Path) -> None:
        db = tmp_path / "db.sqlite3"
        conn = sqlite3.connect(db)
        conn.execute("CREATE TABLE teatree_config_setting (id INTEGER PRIMARY KEY, scope TEXT, key TEXT, value TEXT)")
        conn.execute("INSERT INTO teatree_config_setting (scope, key, value) VALUES ('', 'mode', '{not json')")
        conn.commit()
        conn.close()
        assert cold_reader.read_setting("mode", db_path=db) is None

    def test_unopenable_path_returns_none(self, tmp_path: Path) -> None:
        # A directory exists() but cannot be opened as a RO sqlite DB → fail open.
        a_dir = tmp_path / "a_dir"
        a_dir.mkdir()
        assert cold_reader.read_setting("mode", db_path=a_dir) is None

    def test_locked_db_fails_open_within_busy_timeout(self, tmp_path: Path) -> None:
        db = tmp_path / "db.sqlite3"
        _make_db(db, [("", "mode", "auto")])
        writer = sqlite3.connect(db)
        writer.isolation_level = None
        writer.execute("BEGIN EXCLUSIVE")  # blocks the RO reader's SHARED lock
        try:
            assert cold_reader.read_setting("mode", db_path=db) is None
        finally:
            writer.rollback()
            writer.close()


class TestTypedWrappers:
    @pytest.fixture
    def db(self, tmp_path: Path) -> Path:
        path = tmp_path / "db.sqlite3"
        _make_db(
            path,
            [
                ("", "flag_on", True),
                ("", "flag_off", False),
                ("", "flag_quoted", "false"),
                ("", "budget", 5),
                ("", "budget_bool", True),
                ("", "label", "hello"),
                ("", "label_int", 7),
                ("", "items", ["a", "b"]),
                ("", "nested", {"x": [1, 2], "y": {"z": 3}}),
            ],
        )
        return path

    def test_read_setting_round_trips_every_shape(self, db: Path) -> None:
        assert cold_reader.read_setting("flag_on", db_path=db) is True
        assert cold_reader.read_setting("flag_off", db_path=db) is False
        assert cold_reader.read_setting("budget", db_path=db) == 5
        assert cold_reader.read_setting("label", db_path=db) == "hello"
        assert cold_reader.read_setting("items", db_path=db) == ["a", "b"]
        assert cold_reader.read_setting("nested", db_path=db) == {"x": [1, 2], "y": {"z": 3}}

    def test_bool_setting_strict(self, db: Path) -> None:
        assert cold_reader.bool_setting("flag_on", default=False, db_path=db) is True
        assert cold_reader.bool_setting("flag_off", default=True, db_path=db) is False
        # A quoted "false" is a str, not a bool → default stands (mirrors
        # teatree_settings). default=False makes this anti-vacuous: a naive
        # bool("false") would be truthy and return True, not the default.
        assert cold_reader.bool_setting("flag_quoted", default=False, db_path=db) is False
        assert cold_reader.bool_setting("absent", default=True, db_path=db) is True

    def test_int_setting_strict(self, db: Path) -> None:
        assert cold_reader.int_setting("budget", default=1, db_path=db) == 5
        # A bool is a subclass of int but must be rejected → default. default=99
        # makes this anti-vacuous: a naive int(True) would be 1, not the default.
        assert cold_reader.int_setting("budget_bool", default=99, db_path=db) == 99
        # below minimum → default.
        assert cold_reader.int_setting("budget", default=99, minimum=10, db_path=db) == 99
        assert cold_reader.int_setting("budget", default=99, minimum=5, db_path=db) == 5
        # non-int stored value → default.
        assert cold_reader.int_setting("label", default=42, db_path=db) == 42
        assert cold_reader.int_setting("absent", default=3, db_path=db) == 3

    def test_str_setting_strict(self, db: Path) -> None:
        assert cold_reader.str_setting("label", default="x", db_path=db) == "hello"
        assert cold_reader.str_setting("label_int", default="x", db_path=db) == "x"
        assert cold_reader.str_setting("absent", default="x", db_path=db) == "x"

    def test_list_setting_strict(self, db: Path) -> None:
        assert cold_reader.list_setting("items", default=[], db_path=db) == ["a", "b"]
        assert cold_reader.list_setting("nested", default=["d"], db_path=db) == ["d"]
        assert cold_reader.list_setting("absent", default=["d"], db_path=db) == ["d"]

    def test_wrappers_fail_open_on_missing_db(self, tmp_path: Path) -> None:
        missing = tmp_path / "nope.sqlite3"
        assert cold_reader.bool_setting("x", default=True, db_path=missing) is True
        assert cold_reader.int_setting("x", default=7, db_path=missing) == 7
        assert cold_reader.str_setting("x", default="d", db_path=missing) == "d"
        assert cold_reader.list_setting("x", default=["d"], db_path=missing) == ["d"]


class TestWalNonBlocking:
    def test_committed_value_readable_under_concurrent_writer(self, tmp_path: Path) -> None:
        db = tmp_path / "wal.sqlite3"
        _make_db(db, [("", "mode", "auto")], wal=True)
        writer = sqlite3.connect(db)
        writer.isolation_level = None
        writer.execute("BEGIN IMMEDIATE")
        writer.execute("INSERT INTO teatree_config_setting (scope, key, value) VALUES ('', 'pending', '\"x\"')")
        try:
            # WAL readers see the last committed snapshot without blocking on the writer.
            assert cold_reader.read_setting("mode", db_path=db) == "auto"
            assert cold_reader.read_setting("pending", db_path=db) is None
        finally:
            writer.rollback()
            writer.close()


class TestWalSidecarsAbsent:
    """The quiescent cold state: a WAL-format DB with NO live writer.

    Its ``-wal``/``-shm`` sidecars are absent, so a ``mode=ro``-only open CANNOT
    read it (it would need to recreate the ``-shm`` → ``SQLITE_CANTOPEN``); the
    ``immutable=1`` fallback returns the last-checkpointed value. Distinct from
    ``TestWalNonBlocking``, which holds a writer open and so materializes the
    sidecars — that case passes even on a ``mode=ro``-only reader, masking this
    bug.
    """

    def test_committed_value_readable_with_sidecars_removed(self, tmp_path: Path) -> None:
        db = tmp_path / "wal.sqlite3"
        _make_db(db, [("", "mode", "auto")], wal=True)
        _remove_wal_sidecars(db)
        # Precondition: the realistic cold state — no live sidecars.
        assert not db.with_name(db.name + "-wal").exists()
        assert not db.with_name(db.name + "-shm").exists()
        # mode=ro alone raises SQLITE_CANTOPEN here and the read silently fails
        # open to None; the immutable=1 fallback returns the stored value.
        assert cold_reader.read_setting("mode", db_path=db) == "auto"

    def test_typed_wrappers_do_not_revert_to_default_when_quiescent(self, tmp_path: Path) -> None:
        db = tmp_path / "wal.sqlite3"
        _make_db(db, [("", "budget", 5), ("", "flag", True)], wal=True)
        _remove_wal_sidecars(db)
        # A safety-gate caller's stored kill-switch must NOT revert to its
        # in-code default just because the DB is quiescent. Anti-vacuous: each
        # default differs from the stored value.
        assert cold_reader.int_setting("budget", default=99, db_path=db) == 5
        assert cold_reader.bool_setting("flag", default=False, db_path=db) is True


class TestUriRobustness:
    """An exotic ``T3_CONFIG_DB`` path must not malform the ``file:`` URI.

    A raw ``file:{db}?mode=ro`` f-string breaks on a path containing
    ``%``/``?``/``#`` (it decodes ``%41``→'A' and points at the wrong file →
    silent fail-open); building the URI via ``Path.as_uri()`` percent-encodes
    them.
    """

    def test_reads_value_from_path_with_uri_special_chars(self, tmp_path: Path) -> None:
        # A space AND a literal percent sequence: the raw f-string decodes
        # `%41`→'A' → wrong path → CANTOPEN; as_uri encodes it correctly.
        exotic_dir = tmp_path / "cfg dir %41x"
        exotic_dir.mkdir()
        db = exotic_dir / "db.sqlite3"
        _make_db(db, [("", "mode", "auto")])
        assert cold_reader.read_setting("mode", db_path=db) == "auto"

    def test_reads_via_t3_config_db_env_with_special_chars(self, tmp_path: Path) -> None:
        # Same, resolved through the env hook the cold path actually uses.
        exotic_dir = tmp_path / "x %41 y"
        exotic_dir.mkdir()
        db = exotic_dir / "db.sqlite3"
        _make_db(db, [("", "mode", "auto")])
        env = {"T3_CONFIG_DB": str(db)}
        assert cold_reader.read_setting("mode", env=env) == "auto"


class TestOverlayThenGlobal:
    def test_overlay_shadows_global(self, tmp_path: Path) -> None:
        db = tmp_path / "db.sqlite3"
        _make_db(db, [("", "mode", "interactive"), ("myoverlay", "mode", "auto")])
        assert cold_reader.overlay_then_global("mode", "myoverlay", db_path=db) == "auto"

    def test_falls_back_to_global_when_overlay_absent(self, tmp_path: Path) -> None:
        db = tmp_path / "db.sqlite3"
        _make_db(db, [("", "mode", "interactive")])
        assert cold_reader.overlay_then_global("mode", "myoverlay", db_path=db) == "interactive"

    def test_default_when_neither_present(self, tmp_path: Path) -> None:
        db = tmp_path / "db.sqlite3"
        _make_db(db, [("", "other", "x")])
        assert cold_reader.overlay_then_global("mode", "myoverlay", default="fallback", db_path=db) == "fallback"


class TestMainEntry:
    @pytest.fixture(autouse=True)
    def _canonical_db(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        db = tmp_path / "db.sqlite3"
        _make_db(db, [("", "mode", "auto"), ("", "budget", 5)])
        monkeypatch.setenv("T3_CONFIG_DB", str(db))

    def test_prints_str_value_raw(self, capsys: pytest.CaptureFixture[str]) -> None:
        assert cold_reader.main(["mode"]) == 0
        assert capsys.readouterr().out == "auto\n"

    def test_prints_non_str_value_as_json(self, capsys: pytest.CaptureFixture[str]) -> None:
        assert cold_reader.main(["budget"]) == 0
        assert capsys.readouterr().out == "5\n"

    def test_absent_key_prints_nothing(self, capsys: pytest.CaptureFixture[str]) -> None:
        assert cold_reader.main(["absent"]) == 0
        assert capsys.readouterr().out == ""

    def test_no_args_prints_nothing(self, capsys: pytest.CaptureFixture[str]) -> None:
        assert cold_reader.main([]) == 0
        assert capsys.readouterr().out == ""
