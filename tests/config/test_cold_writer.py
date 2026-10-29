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

import json
import sqlite3
from contextlib import closing
from pathlib import Path

import pytest

from teatree.config import cold_reader, cold_writer
from teatree.config.cold_writer import WriteResult
from teatree.config.host_projection import GENERATION_KEY
from teatree.config.value_visibility import MASKED
from teatree.settings import SQLITE_WRITE_SERIALIZATION_OPTIONS

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
    '"seeded_by" varchar(255) NOT NULL, "written_by" varchar(255) NOT NULL, '
    '"seed_value" text NULL CHECK ((JSON_VALID("seed_value") OR "seed_value" IS NULL)), '
    'CONSTRAINT "uniq_config_setting_scope_key" UNIQUE ("scope", "key"))'
)
_AUDIT_SCHEMA = (
    'CREATE TABLE "teatree_config_setting_change" ('
    '"id" integer NOT NULL PRIMARY KEY AUTOINCREMENT, '
    '"scope" varchar(255) NOT NULL, "key" varchar(255) NOT NULL, '
    '"action" varchar(10) NOT NULL, "old_value" text NULL, "new_value" text NULL, '
    '"written_by" varchar(255) NOT NULL, "changed_at" datetime NOT NULL)'
)


def _make_real_schema_db(path: Path) -> None:
    conn = sqlite3.connect(path)
    try:
        conn.execute(_REAL_SCHEMA)
        conn.commit()
    finally:
        conn.close()


def _make_audited_db_with_non_json_value(path: Path, key: str, raw: str) -> None:
    """A real-schema DB whose stored *key* is not JSON — only reachable by bypassing the CHECK."""
    _make_real_schema_db(path)
    with closing(sqlite3.connect(path)) as conn:
        conn.execute(_AUDIT_SCHEMA)
        conn.execute("PRAGMA ignore_check_constraints=ON")
        conn.execute(
            "INSERT INTO teatree_config_setting "
            "(scope, key, value, created_at, updated_at, seeded_by, written_by, seed_value) "
            "VALUES ('', ?, ?, '', '', '', 'hand-edit', NULL)",
            (key, raw),
        )
        conn.commit()


def _audit_values(path: Path, key: str) -> tuple[str, str]:
    with closing(sqlite3.connect(path)) as conn:
        return conn.execute(
            "SELECT old_value, new_value FROM teatree_config_setting_change WHERE key=? ORDER BY id DESC", (key,)
        ).fetchone()


@pytest.fixture(autouse=True)
def _clean_canonical_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("T3_CONFIG_DB", raising=False)
    monkeypatch.delenv("XDG_DATA_HOME", raising=False)


class TestWriteRoundTrip:
    def test_legacy_schema_keeps_rescue_write_available(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        db = tmp_path / "legacy-schema.sqlite3"
        with closing(sqlite3.connect(db)) as conn:
            conn.execute(
                "CREATE TABLE teatree_config_setting ("
                "scope TEXT NOT NULL, key TEXT NOT NULL, value TEXT NOT NULL, "
                "created_at TEXT NOT NULL, updated_at TEXT NOT NULL, UNIQUE(scope, key))"
            )
            conn.commit()

        outcome = cold_writer.write_setting("issue_implementer_label", "t3-auto", db_path=db)
        assert outcome.result is WriteResult.WROTE
        assert cold_reader.read_setting("issue_implementer_label", db_path=db) == "t3-auto"
        assert "WARNING" in capsys.readouterr().err

    def test_review_phase_refuses_canonical_rescue_write(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        db = tmp_path / "live.sqlite3"
        _make_real_schema_db(db)
        monkeypatch.setenv("T3_CONFIG_DB", str(db))
        monkeypatch.setenv("T3_CONFIG_WRITE_PHASE", "reviewing")
        with pytest.raises(ValueError, match="review or critic"):
            cold_writer.write_setting("autoload", value=False)
        assert cold_reader.read_setting("autoload", db_path=db) is None

    def test_write_then_cold_read(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        db = tmp_path / "db.sqlite3"
        _make_real_schema_db(db)
        monkeypatch.setenv("T3_CONFIG_DB", str(db))

        assert cold_writer.write_setting("autoload", value=False).result is WriteResult.WROTE
        assert cold_reader.read_setting("autoload", scope="") is False

    def test_write_is_an_upsert_not_a_duplicate(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        db = tmp_path / "db.sqlite3"
        _make_real_schema_db(db)
        monkeypatch.setenv("T3_CONFIG_DB", str(db))

        assert cold_writer.write_setting("plan_edit_gate_enabled", value=True).result is WriteResult.WROTE
        assert cold_writer.write_setting("plan_edit_gate_enabled", value=False).result is WriteResult.WROTE
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
        outcome = cold_writer.write_setting("completion_claim_gate_enabled", value=False, db_path=db)
        assert outcome.result is WriteResult.WROTE
        assert outcome.db == db
        assert outcome.detail == ""
        assert cold_reader.read_setting("completion_claim_gate_enabled", scope="", db_path=db) is False

    def test_a_rescue_write_records_the_previous_value_in_the_same_db(self, tmp_path: Path) -> None:
        db = tmp_path / "audited.sqlite3"
        _make_real_schema_db(db)
        with closing(sqlite3.connect(db)) as conn:
            conn.execute(_AUDIT_SCHEMA)
            conn.commit()

        assert cold_writer.write_setting("autoload", value=True, db_path=db).result is WriteResult.WROTE
        assert cold_writer.write_setting("autoload", value=False, db_path=db).result is WriteResult.WROTE
        with closing(sqlite3.connect(db)) as conn:
            rows = conn.execute(
                "SELECT action, old_value, new_value, written_by FROM teatree_config_setting_change "
                "WHERE key='autoload' ORDER BY id"
            ).fetchall()
        assert rows == [("set", None, "true", "cold-writer"), ("set", "true", "false", "cold-writer")]
        with closing(sqlite3.connect(db)) as conn:
            provenance = conn.execute(
                "SELECT seeded_by, written_by FROM teatree_config_setting WHERE key='autoload'"
            ).fetchone()
        assert provenance == ("", "cold-writer")

    def test_unusable_audit_table_keeps_rescue_write(self, tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
        db = tmp_path / "bad-audit.sqlite3"
        _make_real_schema_db(db)
        with closing(sqlite3.connect(db)) as conn:
            conn.execute("CREATE TABLE teatree_config_setting_change (id INTEGER PRIMARY KEY)")
            conn.commit()
        assert cold_writer.write_setting("autoload", value=False, db_path=db).result is WriteResult.WROTE
        assert cold_reader.read_setting("autoload", db_path=db) is False
        assert "audit" in capsys.readouterr().err

    def test_rescue_write_takes_ownership_from_seed(self, tmp_path: Path) -> None:
        db = tmp_path / "seeded.sqlite3"
        _make_real_schema_db(db)
        with closing(sqlite3.connect(db)) as conn:
            conn.execute(
                "INSERT INTO teatree_config_setting "
                "(scope, key, value, created_at, updated_at, seeded_by, written_by, seed_value) "
                "VALUES ('', 'autoload', 'true', '', '', 'deploy', 'deploy', 'true')"
            )
            conn.commit()
        assert cold_writer.write_setting("autoload", value=False, db_path=db).result is WriteResult.WROTE
        with closing(sqlite3.connect(db)) as conn:
            row = conn.execute(
                "SELECT value, seeded_by, written_by, seed_value FROM teatree_config_setting WHERE key='autoload'"
            ).fetchone()
        assert row == ("false", "", "cold-writer", None)

    def test_missing_audit_table_is_loud_but_keeps_the_rescue_write(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        db = tmp_path / "legacy.sqlite3"
        _make_real_schema_db(db)
        assert cold_writer.write_setting("autoload", value=False, db_path=db).result is WriteResult.WROTE
        assert "WARNING" in capsys.readouterr().err

    def test_a_sensitive_rescue_value_is_masked_in_history(self, tmp_path: Path) -> None:
        db = tmp_path / "sensitive.sqlite3"
        _make_real_schema_db(db)
        with closing(sqlite3.connect(db)) as conn:
            conn.execute(_AUDIT_SCHEMA)
            conn.commit()
        value = {"leak": ["private-marker"]}
        assert cold_writer.write_setting("banned_term_registry", value, db_path=db).result is WriteResult.WROTE
        with closing(sqlite3.connect(db)) as conn:
            stored = conn.execute(
                "SELECT new_value FROM teatree_config_setting_change WHERE key='banned_term_registry'"
            ).fetchone()[0]
        assert "private-marker" not in stored

    def test_a_non_json_stored_value_is_repaired_and_kept_in_history(self, tmp_path: Path) -> None:
        db = tmp_path / "hand-edited.sqlite3"
        _make_audited_db_with_non_json_value(db, "autoload", "yes please")

        assert cold_writer.write_setting("autoload", value=False, db_path=db).result is WriteResult.WROTE

        assert cold_reader.read_setting("autoload", db_path=db) is False
        old_value, new_value = _audit_values(db, "autoload")
        assert json.loads(old_value) == "yes please"
        assert new_value == "false"

    def test_a_non_json_stored_secret_is_masked_in_history(self, tmp_path: Path) -> None:
        db = tmp_path / "hand-edited-secret.sqlite3"
        _make_audited_db_with_non_json_value(db, "banned_term_registry", "private-marker")

        outcome = cold_writer.write_setting("banned_term_registry", {"leak": ["other-marker"]}, db_path=db)

        assert outcome.result is WriteResult.WROTE
        old_value, new_value = _audit_values(db, "banned_term_registry")
        assert json.loads(old_value) == MASKED
        assert "private-marker" not in old_value + new_value
        assert "other-marker" not in old_value + new_value


class TestNoDbTierIsClassified:
    """An absent, unmigrated or malformed DB cannot accept a gate write."""

    def test_missing_db_file_is_no_tier(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        absent = tmp_path / "nope.sqlite3"
        monkeypatch.setenv("T3_CONFIG_DB", str(absent))
        assert cold_writer.write_setting("autoload", value=False).result is WriteResult.NO_DB_TIER
        assert not absent.exists()  # the writer must not CREATE the canonical DB

    def test_missing_db_outcome_names_path_and_cause(self, tmp_path: Path) -> None:
        db = tmp_path / "absent.sqlite3"
        outcome = cold_writer.write_setting("autoload", value=False, db_path=db)
        assert outcome.result is WriteResult.NO_DB_TIER
        assert outcome.db == db
        assert outcome.detail == "no file at this path"

    def test_writer_wait_matches_orm(self) -> None:
        assert SQLITE_WRITE_SERIALIZATION_OPTIONS["timeout"] * 1000 == cold_writer._BUSY_TIMEOUT_MS

    def test_missing_table_is_no_tier(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        db = tmp_path / "unmigrated.sqlite3"
        sqlite3.connect(db).close()  # a DB file with NO teatree_config_setting table
        monkeypatch.setenv("T3_CONFIG_DB", str(db))
        outcome = cold_writer.write_setting("autoload", value=False)
        assert outcome.result is WriteResult.NO_DB_TIER
        assert outcome.db == db
        assert outcome.detail == "no teatree_config_setting table"

    def test_malformed_db_is_no_tier(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        garbage = tmp_path / "corrupt.sqlite3"
        garbage.write_bytes(b"this is not a sqlite database at all")
        monkeypatch.setenv("T3_CONFIG_DB", str(garbage))
        outcome = cold_writer.write_setting("autoload", value=False)
        assert outcome.result is WriteResult.NO_DB_TIER
        assert outcome.db == garbage
        assert "not a database" in outcome.detail

    def test_unopenable_db_reports_sqlite_error(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        db = tmp_path / "unopenable.sqlite3"
        db.touch()
        error = sqlite3.OperationalError("unable to open database file")

        def refuse_connect(*_args: object, **_kwargs: object) -> sqlite3.Connection:
            raise error

        monkeypatch.setattr(cold_writer.sqlite3, "connect", refuse_connect)
        outcome = cold_writer.write_setting("autoload", value=False, db_path=db)
        assert outcome.result is WriteResult.NO_DB_TIER
        assert outcome.db == db
        assert outcome.detail == "unable to open database file"


class TestLockedWithRowIsWriteFailed:
    """A locked DB WITH the table present returns ``WRITE_FAILED`` — distinct from ``NO_DB_TIER``.

    This is the residual HIGH (review HOLD #2): when the table exists and a writer holds the
    lock, the seeded row is still authoritative and the reader still returns it, so the write
    failure must NOT be conflated with an absent tier. The table-existence probe,
    not an error-code guess, tells the two apart.
    """

    def test_locked_db_with_table_is_write_failed(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        db = tmp_path / "db.sqlite3"
        _make_real_schema_db(db)
        # Seed a real row so the lock guards an EXISTING value the reader would still return.
        assert cold_writer.write_setting("autoload", value=True, db_path=db).result is WriteResult.WROTE
        monkeypatch.setattr(cold_writer, "_BUSY_TIMEOUT_MS", 100)  # keep the locked test fast
        blocker = sqlite3.connect(db)
        try:
            blocker.execute("PRAGMA journal_mode=WAL")
            blocker.execute("BEGIN IMMEDIATE")  # hold the write lock
            result = cold_writer.write_setting("autoload", value=False, db_path=db)
        finally:
            blocker.close()
        assert result.result is WriteResult.WRITE_FAILED
        assert result.db == db
        assert "database is locked" in result.detail
        # The blocked write did NOT touch the row: the seeded value survives.
        assert cold_reader.read_setting("autoload", scope="", db_path=db) is True


class TestValueAndGenerationCommitTogether:
    """The setting row and its generation bump are ONE transaction, or neither lands.

    The generation labels the projection published from this write. A value that commits
    while its bump is lost is published under a generation every host reader has already
    seen — so the projection carrying the OLD value classifies FRESH, and the write is
    invisible until some later write happens to move the counter.
    """

    @staticmethod
    def _refuse_generation_writes(db: Path) -> None:
        conn = sqlite3.connect(db)
        try:
            conn.execute(
                "CREATE TRIGGER refuse_generation BEFORE INSERT ON teatree_config_setting "
                f"WHEN NEW.key = '{GENERATION_KEY}' "
                "BEGIN SELECT RAISE(ABORT, 'generation write refused'); END"
            )
            conn.commit()
        finally:
            conn.close()

    def test_a_refused_generation_bump_rolls_the_value_back(self, tmp_path: Path) -> None:
        db = tmp_path / "db.sqlite3"
        _make_real_schema_db(db)
        self._refuse_generation_writes(db)

        result = cold_writer.write_setting("autoload", value=False, db_path=db)
        assert result.result is WriteResult.WRITE_FAILED
        assert result.detail == "generation write refused"
        assert cold_reader.read_setting("autoload", scope="", db_path=db) is None

    def test_a_committed_write_carries_its_generation(self, tmp_path: Path) -> None:
        db = tmp_path / "db.sqlite3"
        _make_real_schema_db(db)

        assert cold_writer.write_setting("autoload", value=False, db_path=db).result is WriteResult.WROTE
        assert cold_reader.read_setting(GENERATION_KEY, scope="", db_path=db) == 1
