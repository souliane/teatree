"""``t3 gate`` stays authoritative over a seeded cold-hook row (config-unify PR3 review HIGH).

The coverage gap that hid the bug: every prior cold-hook test built DB rows directly,
so none exercised the interaction between ``t3 gate`` (which wrote TOML) and the
flipped DB-first reader. Once ``t3 setup`` seeded a row, a later ``t3 gate <name>
disable`` was SHADOWED by the seeded row and the never-lockout escape could not be
lifted. The fix gives the cold-hook gates a Django-free DB write path so ``t3 gate``
writes the SAME tier the reader reads.

These tests drive the REAL paths end to end — the ``t3 gate`` Typer command (via the
overlay app), the real ``config_setting import --no-clobber`` seed ``t3 setup`` runs,
and the flipped ``teatree_settings`` reader — against a snapshot of the seeded canonical
DB. No hand-built DB rows: every row originates from the real seed of a planted TOML.
"""

import sqlite3
from io import StringIO
from pathlib import Path

import pytest
from django.core.management import call_command
from django.db import connection
from django.test import TransactionTestCase
from typer.testing import CliRunner

import teatree.config as config_facade
from teatree.cli.overlay import OverlayAppBuilder
from teatree.cli.teatree_gate import memory_recall_gate_is_enabled

_GATE = "memory_recall_enabled"  # a default-ON cold-hook gate
_GATE_PATH = ["gate", "memory-recall"]


def _snapshot_db_to_file(path: Path) -> None:
    dest = sqlite3.connect(path)
    try:
        connection.connection.backup(dest)
    finally:
        dest.close()


class TestGateToggleAfterSeed(TransactionTestCase):
    @pytest.fixture(autouse=True)
    def _setup(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        self.tmp_path = tmp_path
        self.monkeypatch = monkeypatch
        # ``t3 gate``'s ``_config_path()`` is ``Path.home()/.teatree.toml`` and the seed's
        # ``load_config`` reads ``CONFIG_PATH`` — point both at ONE file so the seed reads
        # what ``t3 gate`` (pre-seed, TOML fallback) wrote.
        self.home = tmp_path / "home"
        self.home.mkdir(exist_ok=True)
        self.config_path = self.home / ".teatree.toml"
        monkeypatch.setattr(Path, "home", classmethod(lambda _cls: self.home))
        monkeypatch.setattr(config_facade, "CONFIG_PATH", self.config_path)
        monkeypatch.delenv("T3_CONFIG_DB", raising=False)
        monkeypatch.delenv("XDG_DATA_HOME", raising=False)
        monkeypatch.delenv("T3_OVERLAY_NAME", raising=False)
        monkeypatch.delenv("T3_AUTOLOAD", raising=False)
        self.app = OverlayAppBuilder(overlay_name="acme", project_path=None).build()
        self.runner = CliRunner()

    def _gate(self, *args: str) -> None:
        result = self.runner.invoke(self.app, [*_GATE_PATH, *args])
        assert result.exit_code == 0, result.output

    def _seed_canonical_db(self) -> Path:
        """Run the real ``t3 setup`` seed, then snapshot the seeded DB to the canonical file."""
        call_command("config_setting", "import", "--no-clobber", stdout=StringIO())
        db_file = self.tmp_path / "db.sqlite3"
        _snapshot_db_to_file(db_file)
        self.monkeypatch.setenv("T3_CONFIG_DB", str(db_file))
        return db_file

    def _reader_sees_enabled(self) -> bool:
        from hooks.scripts import teatree_settings  # noqa: PLC0415

        return teatree_settings.teatree_bool_setting(_GATE, default=True)

    def test_enable_after_seed_overrides_the_frozen_row(self) -> None:
        # disable (pre-seed, TOML fallback) -> seed freezes the false row -> enable must
        # override it, not be frozen at the seeded value (toggle authoritative).
        self._gate("disable")
        assert self.config_path.read_text(encoding="utf-8").find("false") != -1  # TOML fallback wrote it
        self._seed_canonical_db()  # DB now carries the frozen false row
        self._gate("enable")  # canonical DB exists -> writes the DB tier
        assert self._reader_sees_enabled() is True
        assert memory_recall_gate_is_enabled() is True  # `t3 gate status` is coherent

    def test_disable_after_seed_lifts_the_escape(self) -> None:
        # The lockout case: a seeded ENABLED gate; a real lockout's `t3 gate disable` must
        # be honoured by the flipped reader, not shadowed by the seeded true row.
        self.config_path.write_text(f"[teatree]\n{_GATE} = true\n", encoding="utf-8")
        self._seed_canonical_db()  # DB carries the frozen true row (gate ON)
        self._gate("disable")  # canonical DB exists -> writes the DB tier
        assert self._reader_sees_enabled() is False
        assert memory_recall_gate_is_enabled() is False  # `t3 gate status` is coherent

    def test_pre_setup_cold_state_falls_back_to_toml(self) -> None:
        # With no canonical DB yet, `t3 gate disable` writes TOML and the reader (no DB)
        # honours it — the pre-setup path is coherent on its own.
        self._gate("disable")
        assert self._reader_sees_enabled() is False

    def test_disable_under_a_locked_canonical_db_fails_loud(self) -> None:
        # The residual HIGH (review HOLD #2): a seeded ENABLED row + a LOCKED canonical DB.
        # The disable's DB write cannot land, so the seeded `true` row survives and the
        # DB-first reader still returns it. Falling back to a TOML write would write a dead,
        # shadowed row and print a lying "gate DISABLED" success while the gate stays ENABLED.
        # The command MUST fail loud (non-zero exit) and print NO success line.
        from teatree.config import cold_writer  # noqa: PLC0415

        self.config_path.write_text(f"[teatree]\n{_GATE} = true\n", encoding="utf-8")
        db_file = self._seed_canonical_db()  # DB carries the frozen true row; T3_CONFIG_DB -> it
        # Keep the busy-wait short so the locked write fails fast instead of waiting 2s.
        self.monkeypatch.setattr(cold_writer, "_BUSY_TIMEOUT_MS", 100)
        blocker = sqlite3.connect(db_file)
        try:
            blocker.execute("PRAGMA journal_mode=WAL")
            blocker.execute("BEGIN IMMEDIATE")  # hold the write lock for the whole invocation
            result = self.runner.invoke(self.app, [*_GATE_PATH, "disable"])
        finally:
            blocker.close()
        assert result.exit_code != 0, result.output
        assert "did NOT take" in result.output  # loud failure, not a silent no-op
        assert "gate DISABLED — wrote" not in result.output  # NO lying success line
        # The seeded row survived: the gate is still ENABLED, coherently across both readers.
        assert self._reader_sees_enabled() is True
        assert memory_recall_gate_is_enabled() is True

    def test_db_write_reports_the_db_destination_not_the_toml_path(self) -> None:
        # The LOW: a successful DB write must report the canonical DB as the destination —
        # previously BOTH the DB-write and the TOML fallback printed the ~/.teatree.toml path.
        self.config_path.write_text(f"[teatree]\n{_GATE} = true\n", encoding="utf-8")
        db_file = self._seed_canonical_db()  # the DB tier now exists, so the toggle lands in it
        result = self.runner.invoke(self.app, [*_GATE_PATH, "disable"])
        assert result.exit_code == 0, result.output
        assert str(db_file) in result.output  # the ACTUAL destination
        assert ".teatree.toml" not in result.output  # never the misreported TOML path

    def test_pre_setup_toml_write_reports_the_toml_destination(self) -> None:
        # The mirror: with no DB tier yet, the TOML write must report the ~/.teatree.toml path.
        result = self.runner.invoke(self.app, [*_GATE_PATH, "disable"])
        assert result.exit_code == 0, result.output
        assert str(self.config_path) in result.output
