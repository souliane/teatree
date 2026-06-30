# test-path: cross-cutting
"""The cold-hook bool readers resolve from the DB store, TOML-fallback (config-unify PR3).

``hooks/scripts/teatree_settings`` is the shared ``[teatree] <flag>`` adapter every
hook-leaf gate reads its kill-switch through. PR3 flips it off TOML and onto the
Django-free ``teatree.config.cold_reader``: a gate flag now resolves from the
canonical ``ConfigSetting`` store FIRST (seeded from ``~/.teatree.toml`` by ``t3
setup``), falling back to the ``[teatree]`` TOML value, then the per-setting
default — the #938 dual-read.

These integration tests build a REAL ``teatree_config_setting`` sqlite file (the
exact Django-migration shape, JSON-encoded values) and read it back through the
LIVE ``teatree_bool_setting`` — no mocks of the read path, so the fail-open and
DB-precedence behaviour is exercised against actual sqlite. The fail-open parity
is the safety crux: a missing/unreadable DB row must fall to the SAME verdict the
TOML reader produced before the flip, so a gate never silently changes its verdict.
"""

import json
import sqlite3
from collections.abc import Iterable
from pathlib import Path

import pytest

from teatree.config import cold_reader

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

    The clean home means a TOML fallback only fires when the test plants
    ``~/.teatree.toml`` itself — so the DB-precedence assertions are not masked by
    a stray host config.
    """
    home = tmp_path / "home"
    home.mkdir(exist_ok=True)
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.delenv("T3_CONFIG_DB", raising=False)
    monkeypatch.delenv("XDG_DATA_HOME", raising=False)
    from hooks.scripts import teatree_settings  # noqa: PLC0415

    return teatree_settings


def _write_teatree_toml(home: Path, body: str) -> None:
    (home / ".teatree.toml").write_text(f"[teatree]\n{body}", encoding="utf-8")


class TestDbValueWins:
    def test_db_disables_a_default_enabled_gate(
        self, settings_module, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        db = tmp_path / "db.sqlite3"
        _make_config_db(db, [("", "memory_recall_enabled", False)])
        monkeypatch.setenv("T3_CONFIG_DB", str(db))
        assert settings_module.teatree_bool_setting("memory_recall_enabled", default=True) is False

    def test_db_enables_a_default_disabled_gate(
        self, settings_module, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        db = tmp_path / "db.sqlite3"
        _make_config_db(db, [("", "dispatch_quote_gate_on_task_create_enabled", True)])
        monkeypatch.setenv("T3_CONFIG_DB", str(db))
        result = settings_module.teatree_bool_setting("dispatch_quote_gate_on_task_create_enabled", default=False)
        assert result is True

    def test_db_value_wins_over_a_conflicting_toml_value(
        self, settings_module, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        # DB precedence: ``config_setting set`` / ``gate disable`` (the DB) must win
        # over a stale ``[teatree]`` TOML value, not be shadowed by it.
        db = tmp_path / "db.sqlite3"
        _make_config_db(db, [("", "memory_recall_enabled", True)])
        monkeypatch.setenv("T3_CONFIG_DB", str(db))
        _write_teatree_toml(tmp_path / "home", "memory_recall_enabled = false\n")
        assert settings_module.teatree_bool_setting("memory_recall_enabled", default=True) is True


class TestFailOpenParity:
    """A missing/unreadable DB row falls to the SAME verdict as before the flip."""

    def test_missing_db_and_no_toml_returns_default(self, settings_module) -> None:
        assert settings_module.teatree_bool_setting("memory_recall_enabled", default=True) is True
        assert (
            settings_module.teatree_bool_setting("dispatch_quote_gate_on_task_create_enabled", default=False) is False
        )

    def test_unreadable_db_fails_open_to_default(
        self, settings_module, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        garbage = tmp_path / "corrupt.sqlite3"
        garbage.write_bytes(b"this is not a sqlite database at all")
        monkeypatch.setenv("T3_CONFIG_DB", str(garbage))
        assert settings_module.teatree_bool_setting("memory_recall_enabled", default=True) is True

    def test_no_silent_gate_disable_when_unconfigured(
        self, settings_module, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        # The crux: a default-enabled gate with a DB present but NO row for it must
        # stay enabled — a missing row never flips the verdict to disabled.
        db = tmp_path / "db.sqlite3"
        _make_config_db(db, [("", "some_other_key", True)])
        monkeypatch.setenv("T3_CONFIG_DB", str(db))
        assert settings_module.teatree_bool_setting("memory_recall_enabled", default=True) is True


class TestTomlFallbackForUnseededKeys:
    """The dual-read fallback preserves a TOML value the import never seeds.

    ``orchestrator_bash_gate_enabled`` is TOML-home (#1775): the TOML->DB import
    skips it, so the DB never carries a row. The TOML fallback is what keeps a
    configured value working — without it the flip would silently re-enable it.
    (``autoload`` is DB-home since eliminate-~/.teatree.toml — read DB-only with NO
    TOML fallback; its DB path is covered in ``tests/config/test_autoload_db``.)
    """

    def test_toml_home_gate_disabled_in_toml_stays_disabled(
        self, settings_module, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        db = tmp_path / "db.sqlite3"
        _make_config_db(db, [("", "some_other_key", True)])  # present DB, no row for the gate
        monkeypatch.setenv("T3_CONFIG_DB", str(db))
        _write_teatree_toml(tmp_path / "home", "orchestrator_bash_gate_enabled = false\n")
        assert settings_module.teatree_bool_setting("orchestrator_bash_gate_enabled", default=True) is False

    def test_db_row_missing_no_toml_returns_default(
        self, settings_module, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        db = tmp_path / "db.sqlite3"
        _make_config_db(db, [("", "some_other_key", True)])
        monkeypatch.setenv("T3_CONFIG_DB", str(db))
        assert settings_module.teatree_bool_setting("orchestrator_bash_gate_enabled", default=True) is True


class TestQuotedStringSemanticsPreserved:
    def test_quoted_false_in_toml_does_not_disable(
        self, settings_module, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        monkeypatch.delenv("T3_CONFIG_DB", raising=False)
        _write_teatree_toml(tmp_path / "home", 'memory_recall_enabled = "false"\n')
        assert settings_module.teatree_bool_setting("memory_recall_enabled", default=True) is True

    def test_non_bool_db_value_falls_through(
        self, settings_module, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        # A JSON string in the DB is not a real bool, so it must not disable a
        # default-true gate — it falls through to the TOML/default verdict.
        db = tmp_path / "db.sqlite3"
        _make_config_db(db, [("", "memory_recall_enabled", "false")])
        monkeypatch.setenv("T3_CONFIG_DB", str(db))
        assert settings_module.teatree_bool_setting("memory_recall_enabled", default=True) is True


class TestNonTeatreeSectionReadsTomlOnly:
    """Only the ``teatree`` section maps to a DB scope; any other section is TOML-only.

    The cold flags all live under ``[teatree]`` (the GLOBAL DB scope). An arbitrary
    section has no DB scope mapping, so a global DB row must never leak into it —
    it reads its ``[section] <flag>`` TOML value, then *default*.
    """

    def test_other_section_reads_its_toml_value_not_the_global_db_row(
        self, settings_module, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        db = tmp_path / "db.sqlite3"
        _make_config_db(db, [("", "flag", True)])  # a GLOBAL row that must NOT leak in
        monkeypatch.setenv("T3_CONFIG_DB", str(db))
        (tmp_path / "home" / ".teatree.toml").write_text("[mysection]\nflag = false\n", encoding="utf-8")
        assert settings_module.section_bool_setting("mysection", "flag", default=True) is False

    def test_other_section_missing_returns_default(self, settings_module) -> None:
        assert settings_module.section_bool_setting("mysection", "flag", default=True) is True


class TestDelegatesToColdReader:
    """Anti-vacuous: patching ``cold_reader.read_setting`` flips the reader's output.

    That flip is observable only if ``teatree_settings`` routes through the cold
    reader — proving the DB read is live, not a no-op left on TOML.
    """

    def test_reader_routes_through_cold_reader(self, settings_module, monkeypatch: pytest.MonkeyPatch) -> None:
        seen: list[tuple[str, str]] = []

        def _fake(name: str, *, scope: str = "", **_: object) -> object:
            seen.append((name, scope))
            return False

        monkeypatch.setattr(cold_reader, "read_setting", _fake)
        assert settings_module.teatree_bool_setting("memory_recall_enabled", default=True) is False
        assert ("memory_recall_enabled", "") in seen

    def test_reader_fails_open_when_the_db_layer_raises(self, settings_module, monkeypatch: pytest.MonkeyPatch) -> None:
        # A raising DB read (a broken import, an unexpected sqlite failure) must
        # never flip a gate's verdict — it degrades to the TOML/default path.
        def _boom(*_args: object, **_kwargs: object) -> object:
            msg = "db layer exploded"
            raise RuntimeError(msg)

        monkeypatch.setattr(cold_reader, "read_setting", _boom)
        assert settings_module.teatree_bool_setting("memory_recall_enabled", default=True) is True
