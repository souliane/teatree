# test-path: cross-cutting
"""Django-free stdlib WRITER for the canonical config store (config-unify PR3).

``teatree.config.cold_writer.write_setting`` is the write-side twin of
``cold_reader`` — the Django-free DB write path ``t3 gate`` uses so its cold-hook
toggle lands on the SAME tier the flipped reader reads. These tests drive it
against a REAL-schema ``teatree_config_setting`` sqlite file (the exact Django
migration shape: NOT-NULL ``created_at``/``updated_at`` and the ``JSON_VALID``
check constraint) and read back through the live ``cold_reader`` — no mocks — so
the upsert, the round-trip, and the three-way :class:`WriteResult` classification
(committed write, absent DB tier, locked-with-row write failure) are exercised
against actual sqlite.
"""

import sqlite3
from pathlib import Path

import pytest

from teatree.config import cold_reader, cold_writer
from teatree.config.cold_writer import WriteResult

# The exact ``teatree_config_setting`` shape Django's migration emits (see
# ``sqlmigrate core 0001_initial``): NOT-NULL timestamp columns + the JSON_VALID
# check + the (scope, key) unique constraint the upsert's ON CONFLICT targets.
_REAL_SCHEMA = (
    'CREATE TABLE "teatree_config_setting" ('
    '"id" integer NOT NULL PRIMARY KEY AUTOINCREMENT, '
    '"scope" varchar(255) NOT NULL, '
    '"key" varchar(255) NOT NULL, '
    '"value" text NOT NULL CHECK ((JSON_VALID("value") OR "value" IS NULL)), '
    '"created_at" datetime NOT NULL, '
    '"updated_at" datetime NOT NULL, '
    'CONSTRAINT "uniq_config_setting_scope_key" UNIQUE ("scope", "key"))'
)


def _make_real_schema_db(path: Path) -> None:
    conn = sqlite3.connect(path)
    try:
        conn.execute(_REAL_SCHEMA)
        conn.commit()
    finally:
        conn.close()


@pytest.fixture(autouse=True)
def _clean_canonical_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("T3_CONFIG_DB", raising=False)
    monkeypatch.delenv("XDG_DATA_HOME", raising=False)


class TestWriteRoundTrip:
    def test_write_then_cold_read(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        db = tmp_path / "db.sqlite3"
        _make_real_schema_db(db)
        monkeypatch.setenv("T3_CONFIG_DB", str(db))

        assert cold_writer.write_setting("memory_recall_enabled", False) is WriteResult.WROTE  # noqa: FBT003
        assert cold_reader.read_setting("memory_recall_enabled", scope="") is False

    def test_write_is_an_upsert_not_a_duplicate(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        db = tmp_path / "db.sqlite3"
        _make_real_schema_db(db)
        monkeypatch.setenv("T3_CONFIG_DB", str(db))

        assert cold_writer.write_setting("plan_edit_gate_enabled", True) is WriteResult.WROTE  # noqa: FBT003
        assert cold_writer.write_setting("plan_edit_gate_enabled", False) is WriteResult.WROTE  # noqa: FBT003
        conn = sqlite3.connect(db)
        try:
            count = conn.execute(
                "SELECT COUNT(*) FROM teatree_config_setting WHERE scope='' AND key='plan_edit_gate_enabled'"
            ).fetchone()[0]
        finally:
            conn.close()
        assert count == 1
        assert cold_reader.read_setting("plan_edit_gate_enabled", scope="") is False

    def test_explicit_db_path_targets_that_file(self, tmp_path: Path) -> None:
        db = tmp_path / "explicit.sqlite3"
        _make_real_schema_db(db)
        assert (
            cold_writer.write_setting("completion_claim_gate_enabled", False, db_path=db)  # noqa: FBT003
            is WriteResult.WROTE
        )
        assert cold_reader.read_setting("completion_claim_gate_enabled", scope="", db_path=db) is False


class TestNoDbTierFallsBackToToml:
    """An absent DB tier returns ``NO_DB_TIER`` so ``t3 gate`` falls back to the TOML write.

    The cold READER also fails open to ``None`` for every one of these cases, so a TOML write
    IS what gets read — falling back to it is correct (and the only never-lockout path).
    """

    def test_missing_db_file_is_no_tier(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        absent = tmp_path / "nope.sqlite3"
        monkeypatch.setenv("T3_CONFIG_DB", str(absent))
        # The pre-``t3 setup`` cold state: no canonical DB yet -> caller writes TOML instead.
        assert cold_writer.write_setting("memory_recall_enabled", False) is WriteResult.NO_DB_TIER  # noqa: FBT003
        assert not absent.exists()  # the writer must not CREATE the canonical DB

    def test_missing_table_is_no_tier(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        db = tmp_path / "unmigrated.sqlite3"
        sqlite3.connect(db).close()  # a DB file with NO teatree_config_setting table
        monkeypatch.setenv("T3_CONFIG_DB", str(db))
        assert cold_writer.write_setting("memory_recall_enabled", False) is WriteResult.NO_DB_TIER  # noqa: FBT003

    def test_malformed_db_is_no_tier(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        garbage = tmp_path / "corrupt.sqlite3"
        garbage.write_bytes(b"this is not a sqlite database at all")
        monkeypatch.setenv("T3_CONFIG_DB", str(garbage))
        assert cold_writer.write_setting("memory_recall_enabled", False) is WriteResult.NO_DB_TIER  # noqa: FBT003


class TestLockedWithRowIsWriteFailed:
    """A locked DB WITH the table present returns ``WRITE_FAILED`` — distinct from ``NO_DB_TIER``.

    This is the residual HIGH (review HOLD #2): when the table exists and a writer holds the
    lock, the seeded row is still authoritative and the reader still returns it, so the write
    failure must NOT be conflated with an absent tier (which would write a dead, shadowed TOML
    row). The table-existence probe — not a sqlite error code — is what tells the two apart.
    """

    def test_locked_db_with_table_is_write_failed(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        db = tmp_path / "db.sqlite3"
        _make_real_schema_db(db)
        # Seed a real row so the lock guards an EXISTING value the reader would still return.
        assert cold_writer.write_setting("memory_recall_enabled", True, db_path=db) is WriteResult.WROTE  # noqa: FBT003
        monkeypatch.setattr(cold_writer, "_BUSY_TIMEOUT_MS", 100)  # fail fast, don't wait 2s
        blocker = sqlite3.connect(db)
        try:
            blocker.execute("PRAGMA journal_mode=WAL")
            blocker.execute("BEGIN IMMEDIATE")  # hold the write lock
            result = cold_writer.write_setting("memory_recall_enabled", False, db_path=db)  # noqa: FBT003
        finally:
            blocker.close()
        assert result is WriteResult.WRITE_FAILED
        # The blocked write did NOT touch the row: the seeded value survives.
        assert cold_reader.read_setting("memory_recall_enabled", scope="", db_path=db) is True
