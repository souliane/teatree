"""The publication gate's banned-terms source fails CLOSED on an unreadable store (#4008).

``_db_banned_terms`` feeds the PUBLIC-target scan vocabulary. Its unset disposition mirrors the
shell scanner's: a genuinely-unset list is the dev/solo no-op (``()``), a deployment that MUST
scrub gets ``None`` (fail closed). A store that could not be READ is neither — it is a broken
control plane, and scanning a public body against no terms because sqlite was busy is the leak
the gate exists to stop. Both gates now route through the ONE
``banned_terms_cli.resolve_unset_verdict`` disposition, so they cannot diverge.
"""

import sqlite3
from pathlib import Path

import pytest

from teatree.core.gates.privacy_gate import _db_banned_terms


@pytest.fixture(autouse=True)
def _no_ambient_banned_terms_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("T3_BANNED_TERMS", raising=False)
    monkeypatch.delenv("T3_BANNED_TERMS_REQUIRED", raising=False)


def test_unreadable_store_fails_closed(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    corrupt = tmp_path / "corrupt.sqlite3"
    corrupt.write_bytes(b"this is not a sqlite database")
    monkeypatch.setenv("T3_CONFIG_DB", str(corrupt))
    assert _db_banned_terms() is None


def test_absent_store_is_the_dev_solo_no_op(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("T3_CONFIG_DB", str(tmp_path / "absent.sqlite3"))
    assert _db_banned_terms() == ()


def test_configured_list_is_returned(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    db = tmp_path / "db.sqlite3"
    conn = sqlite3.connect(str(db))
    conn.execute(
        "CREATE TABLE teatree_config_setting ("
        "id INTEGER PRIMARY KEY, scope TEXT NOT NULL DEFAULT '', key TEXT NOT NULL, value TEXT NOT NULL)"
    )
    conn.execute("INSERT INTO teatree_config_setting (scope, key, value) VALUES ('', 'banned_terms', '[\"acme\"]')")
    conn.commit()
    conn.close()
    monkeypatch.setenv("T3_CONFIG_DB", str(db))
    assert _db_banned_terms() == ("acme",)
