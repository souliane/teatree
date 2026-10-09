"""``t3 <overlay> gate status|disable|enable`` — the self-rescue command (#1474).

The gate subgroup is the orchestrator's guaranteed escape from a heavy-Bash
lockout: it flips the durable DB-home ``orchestrator_bash_gate_enabled``
kill-switch. These tests drive the command through the real overlay Typer app
(the same surface ``t3 <overlay> gate …`` hits) against a real-schema canonical
config DB and assert the DB effect — the Django-free cold read/write IS the
behaviour under test.
"""

import sqlite3
from contextlib import closing
from pathlib import Path

import pytest
import typer
from typer.testing import CliRunner

from teatree.cli.overlay import OverlayAppBuilder
from teatree.cli.teatree_gate import (
    COMPLETION_CLAIM_GATE_KEY,
    CONFIG_OVERWRITE_GATE_KEY,
    GATE_KEY,
    GATE_RELAXATION_GATE_KEY,
    MAIN_CLONE_GATE_KEY,
    _gate_key_is_enabled,
    gate_is_enabled,
)
from teatree.config import cold_reader, cold_writer

# The exact ``teatree_config_setting`` shape Django's migration emits — the
# NOT-NULL timestamp columns and the (scope, key) unique constraint the cold
# writer's ON CONFLICT targets.
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


@pytest.fixture
def canonical_db(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    db = tmp_path / "db.sqlite3"
    conn = sqlite3.connect(db)
    try:
        conn.execute(_REAL_SCHEMA)
        conn.commit()
    finally:
        conn.close()
    monkeypatch.setenv("T3_CONFIG_DB", str(db))
    return db


@pytest.fixture
def app() -> typer.Typer:
    return OverlayAppBuilder(overlay_name="acme", project_path=None).build()


def _gate_value(db: Path, key: str) -> object:
    return cold_reader.read_setting(key, scope="", db_path=db)


class TestGateStatus:
    def test_status_reports_enabled_when_no_db(self, app: typer.Typer) -> None:
        result = CliRunner().invoke(app, ["gate", "status"])
        assert result.exit_code == 0, result.output
        assert "ENABLED" in result.output

    def test_heavy_bash_status_keeps_its_own_wording_after_shared_refactor(self, app: typer.Typer) -> None:
        """F3.4: the top-level heavy-Bash gate now shares ``_attach_gate_commands``.

        The extraction must leave its distinctive enabled-status wording intact —
        the keyed gates echo a bare ``gate ENABLED``, but this one keeps its
        ``heavy orchestrator bash blocked`` qualifier.
        """
        result = CliRunner().invoke(app, ["gate", "status"])
        assert result.exit_code == 0, result.output
        assert "heavy orchestrator bash blocked" in result.output

    def test_status_reports_disabled_after_disable(self, app: typer.Typer, canonical_db: Path) -> None:
        assert CliRunner().invoke(app, ["gate", "disable"]).exit_code == 0
        result = CliRunner().invoke(app, ["gate", "status"])
        assert result.exit_code == 0, result.output
        assert "DISABLED" in result.output


class TestGateDisableEnable:
    def test_disable_fails_loudly_when_the_canonical_db_is_missing(self, app: typer.Typer) -> None:
        result = CliRunner().invoke(app, ["gate", "disable"])
        assert result.exit_code == 1
        assert "did NOT take" in result.output

    def test_disable_fails_loudly_when_a_seeded_row_stays_enabled(
        self, app: typer.Typer, canonical_db: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        with closing(sqlite3.connect(canonical_db)) as conn, conn:
            conn.execute(
                "INSERT INTO teatree_config_setting (scope, key, value, created_at, updated_at) "
                "VALUES ('', ?, 'true', '2026-01-01 00:00:00.0', '2026-01-01 00:00:00.0')",
                (GATE_KEY,),
            )
        monkeypatch.setattr(cold_writer, "write_setting", lambda *_args, **_kwargs: None)
        result = CliRunner().invoke(app, ["gate", "disable"])
        assert result.exit_code == 1
        assert "did NOT take" in result.output
        assert _gate_value(canonical_db, GATE_KEY) is True

    def test_disable_writes_false(self, app: typer.Typer, canonical_db: Path) -> None:
        result = CliRunner().invoke(app, ["gate", "disable"])
        assert result.exit_code == 0, result.output
        assert _gate_value(canonical_db, GATE_KEY) is False
        assert gate_is_enabled() is False

    def test_enable_writes_true(self, app: typer.Typer, canonical_db: Path) -> None:
        result = CliRunner().invoke(app, ["gate", "enable"])
        assert result.exit_code == 0, result.output
        assert _gate_value(canonical_db, GATE_KEY) is True
        assert gate_is_enabled() is True

    def test_disable_then_enable_round_trips(self, app: typer.Typer, canonical_db: Path) -> None:
        runner = CliRunner()
        assert runner.invoke(app, ["gate", "disable"]).exit_code == 0
        assert gate_is_enabled() is False
        assert runner.invoke(app, ["gate", "enable"]).exit_code == 0
        assert gate_is_enabled() is True


class TestGateIsEnabledFailsOpen:
    """``gate_is_enabled`` fails OPEN so the reported status matches the gate."""

    def test_enabled_with_no_db(self) -> None:
        assert gate_is_enabled() is True

    def test_enabled_on_non_bool_value(self, canonical_db: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        conn = sqlite3.connect(canonical_db)
        conn.execute(
            "INSERT INTO teatree_config_setting (scope, key, value, created_at, updated_at) "
            "VALUES ('', ?, '\"oops\"', '2026-01-01 00:00:00.0', '2026-01-01 00:00:00.0')",
            (GATE_KEY,),
        )
        conn.commit()
        conn.close()
        assert gate_is_enabled() is True


class TestConfigOverwriteGate:
    """``t3 <overlay> gate config-overwrite disable|enable`` — the PR #2661 self-rescue."""

    def test_disable_writes_false_and_is_reflected(self, app: typer.Typer, canonical_db: Path) -> None:
        result = CliRunner().invoke(app, ["gate", "config-overwrite", "disable"])
        assert result.exit_code == 0, result.output
        assert _gate_value(canonical_db, CONFIG_OVERWRITE_GATE_KEY) is False
        assert _gate_key_is_enabled(CONFIG_OVERWRITE_GATE_KEY) is False

    def test_enabled_by_default_when_no_db(self) -> None:
        assert _gate_key_is_enabled(CONFIG_OVERWRITE_GATE_KEY) is True

    def test_round_trips(self, app: typer.Typer, canonical_db: Path) -> None:
        runner = CliRunner()
        assert runner.invoke(app, ["gate", "config-overwrite", "disable"]).exit_code == 0
        assert _gate_key_is_enabled(CONFIG_OVERWRITE_GATE_KEY) is False
        assert runner.invoke(app, ["gate", "config-overwrite", "enable"]).exit_code == 0
        assert _gate_key_is_enabled(CONFIG_OVERWRITE_GATE_KEY) is True


class TestCompletionClaimGate:
    """``t3 <overlay> gate completion-claim disable|enable`` — the #2665 self-rescue."""

    def test_disable_writes_false_and_is_reflected(self, app: typer.Typer, canonical_db: Path) -> None:
        result = CliRunner().invoke(app, ["gate", "completion-claim", "disable"])
        assert result.exit_code == 0, result.output
        assert _gate_value(canonical_db, COMPLETION_CLAIM_GATE_KEY) is False
        assert _gate_key_is_enabled(COMPLETION_CLAIM_GATE_KEY) is False

    def test_enabled_by_default_when_no_db(self) -> None:
        assert _gate_key_is_enabled(COMPLETION_CLAIM_GATE_KEY) is True

    def test_round_trips(self, app: typer.Typer, canonical_db: Path) -> None:
        runner = CliRunner()
        assert runner.invoke(app, ["gate", "completion-claim", "disable"]).exit_code == 0
        assert _gate_key_is_enabled(COMPLETION_CLAIM_GATE_KEY) is False
        assert runner.invoke(app, ["gate", "completion-claim", "enable"]).exit_code == 0
        assert _gate_key_is_enabled(COMPLETION_CLAIM_GATE_KEY) is True


class TestMainCloneGate:
    """``t3 <overlay> gate main-clone disable|enable`` — the #2836 self-rescue (#2844 #3)."""

    def test_disable_writes_false_and_is_reflected(self, app: typer.Typer, canonical_db: Path) -> None:
        result = CliRunner().invoke(app, ["gate", "main-clone", "disable"])
        assert result.exit_code == 0, result.output
        assert _gate_value(canonical_db, MAIN_CLONE_GATE_KEY) is False
        assert _gate_key_is_enabled(MAIN_CLONE_GATE_KEY) is False

    def test_enabled_by_default_when_no_db(self) -> None:
        assert _gate_key_is_enabled(MAIN_CLONE_GATE_KEY) is True

    def test_status_reports_state(self, app: typer.Typer, canonical_db: Path) -> None:
        runner = CliRunner()
        assert "ENABLED" in runner.invoke(app, ["gate", "main-clone", "status"]).output
        runner.invoke(app, ["gate", "main-clone", "disable"])
        assert "DISABLED" in runner.invoke(app, ["gate", "main-clone", "status"]).output

    def test_round_trips(self, app: typer.Typer, canonical_db: Path) -> None:
        runner = CliRunner()
        assert runner.invoke(app, ["gate", "main-clone", "disable"]).exit_code == 0
        assert _gate_key_is_enabled(MAIN_CLONE_GATE_KEY) is False
        assert runner.invoke(app, ["gate", "main-clone", "enable"]).exit_code == 0
        assert _gate_key_is_enabled(MAIN_CLONE_GATE_KEY) is True


class TestGateRelaxationGate:
    """``t3 <overlay> gate gate-relaxation disable|enable`` — the anti-relaxation self-rescue (#850)."""

    def test_disable_writes_false_and_is_reflected(self, app: typer.Typer, canonical_db: Path) -> None:
        result = CliRunner().invoke(app, ["gate", "gate-relaxation", "disable"])
        assert result.exit_code == 0, result.output
        assert _gate_value(canonical_db, GATE_RELAXATION_GATE_KEY) is False
        assert _gate_key_is_enabled(GATE_RELAXATION_GATE_KEY) is False

    def test_enabled_by_default_when_no_db(self) -> None:
        assert _gate_key_is_enabled(GATE_RELAXATION_GATE_KEY) is True

    def test_status_reports_state(self, app: typer.Typer, canonical_db: Path) -> None:
        runner = CliRunner()
        assert "ENABLED" in runner.invoke(app, ["gate", "gate-relaxation", "status"]).output
        runner.invoke(app, ["gate", "gate-relaxation", "disable"])
        assert "DISABLED" in runner.invoke(app, ["gate", "gate-relaxation", "status"]).output

    def test_round_trips(self, app: typer.Typer, canonical_db: Path) -> None:
        runner = CliRunner()
        assert runner.invoke(app, ["gate", "gate-relaxation", "disable"]).exit_code == 0
        assert _gate_key_is_enabled(GATE_RELAXATION_GATE_KEY) is False
        assert runner.invoke(app, ["gate", "gate-relaxation", "enable"]).exit_code == 0
        assert _gate_key_is_enabled(GATE_RELAXATION_GATE_KEY) is True


class TestDbRowIsolation:
    def test_disable_preserves_other_db_rows(self, app: typer.Typer, canonical_db: Path) -> None:
        conn = sqlite3.connect(canonical_db)
        conn.execute(
            "INSERT INTO teatree_config_setting (scope, key, value, created_at, updated_at) "
            "VALUES ('', 'mode', '\"auto\"', '2026-01-01 00:00:00.0', '2026-01-01 00:00:00.0')"
        )
        conn.commit()
        conn.close()
        result = CliRunner().invoke(app, ["gate", "disable"])
        assert result.exit_code == 0, result.output
        assert _gate_value(canonical_db, GATE_KEY) is False
        assert _gate_value(canonical_db, "mode") == "auto"
