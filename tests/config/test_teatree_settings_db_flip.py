# test-path: cross-cutting
"""The cold-hook bool readers resolve from the DB store (config-unify PR3).

``hooks/scripts/teatree_settings`` is the shared ``<flag>`` adapter every hook-leaf
gate reads its kill-switch through. A gate flag resolves from the canonical
``ConfigSetting`` store via the Django-free ``teatree.config.cold_reader``, then the
per-setting default.

These integration tests build a REAL ``teatree_config_setting`` sqlite file (the
exact Django-migration shape, JSON-encoded values) and read it back through the
LIVE ``teatree_bool_setting`` — no mocks of the read path, so the fail-open and
DB-resolution behaviour is exercised against actual sqlite. A missing/unreadable DB
row must fall to the per-setting default, so a gate never silently changes its
verdict.
"""

import json
import sqlite3
from collections.abc import Iterable
from pathlib import Path

import pytest

Row = tuple[str, str, object]


def _make_config_db(path: Path, rows: Iterable[Row]) -> None:
    """Build a real ``teatree_config_setting`` DB matching the Django migration."""
    conn = sqlite3.connect(path)
    try:
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


@pytest.fixture
def settings_module(monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
    """The live ``teatree_settings`` adapter with ``$HOME`` at a clean tmp dir.

    Clearing ``T3_CONFIG_DB`` / ``XDG_DATA_HOME`` means the cold reader resolves under
    the isolated ``$HOME`` and never reads a host DB, so DB-resolution assertions are
    not masked by stray host config.
    """
    home = tmp_path / "home"
    home.mkdir(exist_ok=True)
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.delenv("T3_CONFIG_DB", raising=False)
    monkeypatch.delenv("XDG_DATA_HOME", raising=False)
    from hooks.scripts import teatree_settings  # noqa: PLC0415

    return teatree_settings


class TestFailOpenParity:
    """A missing/unreadable DB row falls to the per-setting default."""

    def test_db_row_missing_returns_default(
        self, settings_module, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        db = tmp_path / "db.sqlite3"
        _make_config_db(db, [("", "some_other_key", True)])
        monkeypatch.setenv("T3_CONFIG_DB", str(db))
        assert settings_module.teatree_bool_setting("orchestrator_bash_gate_enabled", default=True) is True


class TestNonTeatreeSection:
    """Only the ``teatree`` section maps to a DB scope; any other section has none."""

    def test_other_section_missing_returns_default(self, settings_module) -> None:
        assert settings_module.section_bool_setting("mysection", "flag", default=True) is True
