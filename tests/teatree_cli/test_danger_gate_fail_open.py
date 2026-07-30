"""``t3 review gate fail-open enable|disable|status`` — the master fail-open switch.

The over-deny gates (quote-scanner / banned-terms on a PRIVATE surface,
validate-mr broken-env, skill-loading, protect-default-branch,
block-uncovered-diff, agent-plan-gate) can wedge the factory when their
detection misbehaves. ``danger_gate_fail_open`` is the DB-home master switch that
flips ALL of them to fail-open at once. It is OFF by default — the gates keep
their protective posture unless the operator deliberately turns the escape hatch
on. The ``danger_`` prefix makes a forgotten ``true`` unmissable.

These tests drive the command through the real ``review`` Typer app (the same
surface ``t3 review gate fail-open …`` hits) against a real-schema canonical
config DB and assert the DB effect — the Django-free cold read/write IS the
behaviour under test.
"""

import sqlite3
from pathlib import Path

import pytest
from typer.testing import CliRunner

from teatree.cli.review import review_app
from teatree.cli.teatree_gate import DANGER_GATE_FAIL_OPEN_KEY, danger_gate_fail_open_is_enabled
from teatree.config import cold_reader

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


def _fail_open_value(db: Path) -> object:
    return cold_reader.read_setting(DANGER_GATE_FAIL_OPEN_KEY, scope="", db_path=db)


class TestDangerPrefixedKey:
    def test_key_is_danger_prefixed(self) -> None:
        assert DANGER_GATE_FAIL_OPEN_KEY == "danger_gate_fail_open"

    def test_enable_writes_the_danger_prefixed_key(self, canonical_db: Path) -> None:
        result = CliRunner().invoke(review_app, ["gate", "fail-open", "enable"])
        assert result.exit_code == 0, result.output
        assert _fail_open_value(canonical_db) is True
        assert cold_reader.read_setting("gate_fail_open", scope="", db_path=canonical_db) is None


class TestDefaultOff:
    def test_disabled_when_no_db(self) -> None:
        assert danger_gate_fail_open_is_enabled() is False

    def test_disabled_when_key_absent(self, canonical_db: Path) -> None:
        _seed_row(canonical_db, "mode", '"auto"')
        assert danger_gate_fail_open_is_enabled() is False

    def test_status_reports_off_by_default(self) -> None:
        result = CliRunner().invoke(review_app, ["gate", "fail-open", "status"])
        assert result.exit_code == 0, result.output
        assert "fail-open OFF" in result.output


class TestEnableDisable:
    def test_enable_writes_true_and_resolver_agrees(self, canonical_db: Path) -> None:
        result = CliRunner().invoke(review_app, ["gate", "fail-open", "enable"])
        assert result.exit_code == 0, result.output
        assert _fail_open_value(canonical_db) is True
        assert danger_gate_fail_open_is_enabled() is True

    def test_disable_writes_false_and_resolver_agrees(self, canonical_db: Path) -> None:
        _seed_row(canonical_db, DANGER_GATE_FAIL_OPEN_KEY, "true")
        result = CliRunner().invoke(review_app, ["gate", "fail-open", "disable"])
        assert result.exit_code == 0, result.output
        assert _fail_open_value(canonical_db) is False
        assert danger_gate_fail_open_is_enabled() is False

    def test_enable_then_disable_round_trips(self, canonical_db: Path) -> None:
        runner = CliRunner()
        assert runner.invoke(review_app, ["gate", "fail-open", "enable"]).exit_code == 0
        assert danger_gate_fail_open_is_enabled() is True
        assert runner.invoke(review_app, ["gate", "fail-open", "disable"]).exit_code == 0
        assert danger_gate_fail_open_is_enabled() is False

    def test_status_reports_on_after_enable(self, canonical_db: Path) -> None:
        runner = CliRunner()
        assert runner.invoke(review_app, ["gate", "fail-open", "enable"]).exit_code == 0
        result = runner.invoke(review_app, ["gate", "fail-open", "status"])
        assert result.exit_code == 0, result.output
        assert "fail-open ON" in result.output


class TestResolverFailsClosed:
    """The master switch is OFF unless an explicit ``true`` is recorded.

    Unlike the kill-switch keys (which fail OPEN to enabled), the fail-open
    master switch fails CLOSED to disabled on a missing/odd value — a broken
    read must never silently relax every gate.
    """

    def test_off_with_no_db(self) -> None:
        assert danger_gate_fail_open_is_enabled() is False

    def test_off_on_non_bool_value(self, canonical_db: Path) -> None:
        _seed_row(canonical_db, DANGER_GATE_FAIL_OPEN_KEY, '"oops"')
        assert danger_gate_fail_open_is_enabled() is False

    def test_off_when_only_old_key_present(self, canonical_db: Path) -> None:
        _seed_row(canonical_db, "gate_fail_open", "true")
        assert danger_gate_fail_open_is_enabled() is False


class TestDbRowIsolation:
    def test_enable_preserves_other_db_rows(self, canonical_db: Path) -> None:
        _seed_row(canonical_db, "mode", '"auto"')
        result = CliRunner().invoke(review_app, ["gate", "fail-open", "enable"])
        assert result.exit_code == 0, result.output
        assert _fail_open_value(canonical_db) is True
        assert cold_reader.read_setting("mode", scope="", db_path=canonical_db) == "auto"
