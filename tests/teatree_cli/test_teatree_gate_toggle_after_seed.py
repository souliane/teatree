"""``t3 gate`` is authoritative over an existing DB row (config-unify).

Every gate key is DB-home: ``t3 gate <name> enable/disable`` writes the canonical
config DB via the Django-free cold writer and the flipped hook reader reads that
SAME tier. These tests drive the REAL paths end to end — the ``t3 gate`` Typer
command (via the overlay app) against a real-schema canonical DB — and prove the
toggle overrides a pre-existing row, and that a write which cannot land (no DB
tier, or a locked DB) fails LOUD rather than printing a lying success line.
"""

import sqlite3
from pathlib import Path

import pytest
from typer.testing import CliRunner

from teatree.cli.overlay import OverlayAppBuilder
from teatree.cli.teatree_gate import memory_recall_gate_is_enabled
from teatree.config import cold_reader, cold_writer

_GATE = "memory_recall_enabled"  # a default-ON cold-hook gate
_GATE_PATH = ["gate", "memory-recall"]

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


def _seed_row(db: Path, key: str, json_value: str) -> None:
    conn = sqlite3.connect(db)
    try:
        conn.execute(
            "INSERT INTO teatree_config_setting (scope, key, value, created_at, updated_at) "
            "VALUES ('', ?, ?, '2026-01-01 00:00:00.0', '2026-01-01 00:00:00.0')",
            (key, json_value),
        )
        conn.commit()
    finally:
        conn.close()


class TestGateToggle:
    @pytest.fixture(autouse=True)
    def _setup(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        self.tmp_path = tmp_path
        self.monkeypatch = monkeypatch
        self.app = OverlayAppBuilder(overlay_name="acme", project_path=None).build()
        self.runner = CliRunner()
        monkeypatch.delenv("T3_OVERLAY_NAME", raising=False)
        monkeypatch.delenv("T3_AUTOLOAD", raising=False)

    def _canonical_db(self) -> Path:
        db = self.tmp_path / "db.sqlite3"
        conn = sqlite3.connect(db)
        try:
            conn.execute(_REAL_SCHEMA)
            conn.commit()
        finally:
            conn.close()
        self.monkeypatch.setenv("T3_CONFIG_DB", str(db))
        return db

    def _gate(self, *args: str) -> None:
        result = self.runner.invoke(self.app, [*_GATE_PATH, *args])
        assert result.exit_code == 0, result.output

    def _reader_sees_enabled(self) -> bool:
        from hooks.scripts import teatree_settings  # noqa: PLC0415

        return teatree_settings.teatree_bool_setting(_GATE, default=True)

    def test_disable_overrides_a_seeded_enabled_row(self) -> None:
        db = self._canonical_db()
        _seed_row(db, _GATE, "true")  # DB carries an ENABLED row
        self._gate("disable")  # the toggle must override it
        assert self._reader_sees_enabled() is False
        assert memory_recall_gate_is_enabled() is False  # `t3 gate status` is coherent

    def test_enable_overrides_a_seeded_disabled_row(self) -> None:
        db = self._canonical_db()
        _seed_row(db, _GATE, "false")  # DB carries a DISABLED row
        self._gate("enable")
        assert self._reader_sees_enabled() is True
        assert memory_recall_gate_is_enabled() is True

    def test_pre_setup_no_db_fails_loud(self) -> None:
        # No canonical DB (fresh, pre-``t3 setup`` state): the write cannot land, so
        # the read-back-verify catches the no-op and the command fails LOUD rather
        # than printing a lying "gate DISABLED" success line.
        result = self.runner.invoke(self.app, [*_GATE_PATH, "disable"])
        assert result.exit_code != 0, result.output
        assert "did NOT take" in result.output
        assert "gate DISABLED — wrote" not in result.output

    def test_disable_under_a_locked_canonical_db_fails_loud(self) -> None:
        # A seeded ENABLED row + a LOCKED canonical DB: the disable's DB write cannot
        # land, so the seeded `true` row survives and the DB-first reader still returns
        # it. The command MUST fail loud (non-zero exit) and print NO success line.
        db = self._canonical_db()
        _seed_row(db, _GATE, "true")
        # Keep the busy-wait short so the locked write fails fast instead of waiting 2s.
        self.monkeypatch.setattr(cold_writer, "_BUSY_TIMEOUT_MS", 100)
        blocker = sqlite3.connect(db)
        try:
            blocker.execute("PRAGMA journal_mode=WAL")
            blocker.execute("BEGIN IMMEDIATE")  # hold the write lock for the whole invocation
            result = self.runner.invoke(self.app, [*_GATE_PATH, "disable"])
        finally:
            blocker.close()
        assert result.exit_code != 0, result.output
        assert "did NOT take" in result.output
        assert "gate DISABLED — wrote" not in result.output
        # The seeded row survived: the gate is still ENABLED, coherently across both readers.
        assert self._reader_sees_enabled() is True
        assert memory_recall_gate_is_enabled() is True

    def test_write_reports_the_db_destination(self) -> None:
        db = self._canonical_db()
        _seed_row(db, _GATE, "true")
        result = self.runner.invoke(self.app, [*_GATE_PATH, "disable"])
        assert result.exit_code == 0, result.output
        assert str(db) in result.output  # the ACTUAL destination — the canonical DB
        assert cold_reader.read_setting(_GATE, scope="", db_path=db) is False
