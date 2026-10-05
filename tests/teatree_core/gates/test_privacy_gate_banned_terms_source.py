"""The publication gate's banned-terms source fails closed without a readable registry."""

import sqlite3
from pathlib import Path

import pytest

from teatree.core.gates.privacy_gate import _db_banned_terms


@pytest.fixture(autouse=True)
def _no_ambient_banned_terms_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("TEATREE_TERM_REGISTRY", raising=False)


def test_unreadable_store_fails_closed(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    corrupt = tmp_path / "corrupt.sqlite3"
    corrupt.write_bytes(b"this is not a sqlite database")
    monkeypatch.setenv("T3_CONFIG_DB", str(corrupt))
    assert _db_banned_terms() is None


def test_absent_store_fails_closed(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("T3_CONFIG_DB", str(tmp_path / "absent.sqlite3"))
    assert _db_banned_terms() is None


def test_configured_list_is_returned(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    db = tmp_path / "db.sqlite3"
    conn = sqlite3.connect(str(db))
    conn.execute(
        "CREATE TABLE teatree_config_setting ("
        "id INTEGER PRIMARY KEY, scope TEXT NOT NULL DEFAULT '', key TEXT NOT NULL, value TEXT NOT NULL)"
    )
    conn.execute(
        "INSERT INTO teatree_config_setting (scope, key, value) VALUES ('', 'banned_term_registry', ?)",
        ('{"leak": ["acme"], "prose_collider": ["acme"]}',),
    )
    conn.commit()
    conn.close()
    monkeypatch.setenv("T3_CONFIG_DB", str(db))
    assert _db_banned_terms() == ("acme",)
