# test-path: cross-cutting
"""The five settings keys that still carry the legacy word keep resolving under that exact name.

A row an operator stored under one of them (the live control DB holds one for
``orchestrator_bash_gate_enabled``) is read by hook processes that never start Django, by
hosts that only see the published projection, and by the Django resolver. Renaming a key
orphans that row and silently re-arms a gate its owner switched off, so every name is
written out literally here: whoever renames one has to edit this file on purpose.
"""

import json
import sqlite3
import tomllib
from io import StringIO
from pathlib import Path

import pytest
from django.core.management import call_command
from django.test import TestCase

from teatree.config import cold_db, cold_reader
from teatree.config.cold_defaults import DEFAULTS_TOML
from teatree.config.host_projection import ProjectionPublisher
from teatree.config.known_settings import ALL_KNOWN_CONFIG_SETTINGS
from teatree.config.resolution import NO_EFFECTIVE_DEFAULT, effective_default, get_effective_setting

GATE_KEYS = (
    "orchestrator_bash_gate_enabled",
    "orchestrator_boundary_agent_gate_enabled",
    "orchestrator_delegation_gate_enabled",
)
BUDGET_KEYS = ("orchestrator_turn_budget", "orchestrator_turn_wall_clock_seconds")
LEGACY_KEYS = (*GATE_KEYS, *BUDGET_KEYS)
DECLARED_KEYS = Path(__file__).resolve().parents[2] / "src" / "teatree" / "config" / "declared_keys.txt"
STORED_BUDGET = 7

_SETTINGS_SCHEMA = """
CREATE TABLE teatree_config_setting (
    id INTEGER PRIMARY KEY,
    scope TEXT NOT NULL,
    key TEXT NOT NULL,
    value TEXT NOT NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    UNIQUE (scope, key)
);
CREATE TABLE teatree_loop_state (id INTEGER PRIMARY KEY, name TEXT NOT NULL UNIQUE, status TEXT NOT NULL);
"""


def stored_legacy_rows() -> dict[str, object]:
    return {**dict.fromkeys(GATE_KEYS, False), **dict.fromkeys(BUDGET_KEYS, STORED_BUDGET)}


def build_config_db(path: Path, rows: dict[str, object]) -> Path:
    conn = sqlite3.connect(path)
    try:
        conn.executescript(_SETTINGS_SCHEMA)
        conn.executemany(
            "INSERT INTO teatree_config_setting (scope, key, value, created_at, updated_at) VALUES ('', ?, ?, '', '')",
            [(key, json.dumps(value)) for key, value in rows.items()],
        )
        conn.commit()
    finally:
        conn.close()
    return path


def read_typed(key: str, *, db_path: Path | None = None) -> object:
    reader = cold_reader.bool_setting if key in GATE_KEYS else cold_reader.int_setting
    return reader(key, db_path=db_path)


def shipped_toml_keys(table: dict[str, object]) -> dict[str, object]:
    flat: dict[str, object] = {}
    for key, value in table.items():
        if isinstance(value, dict):
            flat |= shipped_toml_keys(value)
        else:
            flat[key] = value
    return flat


@pytest.fixture
def stored_db(tmp_path: Path) -> Path:
    return build_config_db(tmp_path / "stored.sqlite3", stored_legacy_rows())


@pytest.mark.parametrize("key", LEGACY_KEYS)
def test_the_name_is_declared_everywhere_a_reader_looks_it_up(key: str) -> None:
    manifest = set(DECLARED_KEYS.read_text(encoding="utf-8").splitlines())
    shipped = shipped_toml_keys(tomllib.loads(DEFAULTS_TOML.read_text(encoding="utf-8"))["teatree"])

    assert key in manifest
    assert key in ALL_KNOWN_CONFIG_SETTINGS
    assert shipped[key] == effective_default(key) != NO_EFFECTIVE_DEFAULT


@pytest.mark.parametrize("key", LEGACY_KEYS)
def test_the_cold_hook_reader_honours_a_row_stored_under_the_name(key: str, stored_db: Path, tmp_path: Path) -> None:
    empty_db = build_config_db(tmp_path / "empty.sqlite3", {})

    assert read_typed(key, db_path=empty_db) == effective_default(key)
    assert read_typed(key, db_path=stored_db) == stored_legacy_rows()[key]


@pytest.mark.parametrize("key", LEGACY_KEYS)
def test_the_host_projection_read_honours_a_row_stored_under_the_name(
    key: str, stored_db: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    ProjectionPublisher(stored_db, data_dir).publish()
    monkeypatch.setattr(cold_db, "canonical_data_dir", lambda **_: data_dir)
    monkeypatch.setattr(cold_db, "canonical_config_db", lambda **_: tmp_path / "absent" / "db.sqlite3")

    assert read_typed(key) == stored_legacy_rows()[key]


class TestTheHotResolver(TestCase):
    def _get(self, key: str) -> str:
        out = StringIO()
        call_command("config_setting", "get", key, stdout=out)
        return out.getvalue()

    def test_the_bash_gate_field_resolves_a_row_stored_under_its_name(self) -> None:
        assert get_effective_setting("orchestrator_bash_gate_enabled") is True

        call_command("config_setting", "set", "orchestrator_bash_gate_enabled", "false", stdout=StringIO())

        assert get_effective_setting("orchestrator_bash_gate_enabled") is False

    def test_every_name_is_settable_and_readable_through_the_command(self) -> None:
        for key in LEGACY_KEYS:
            stored = json.dumps(stored_legacy_rows()[key])

            call_command("config_setting", "set", key, stored, stdout=StringIO())

            assert f"{key} = {stored_legacy_rows()[key]}" in self._get(key)
