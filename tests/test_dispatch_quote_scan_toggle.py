"""The pre-dispatch quote scan carries a kill-switch that fails LOUD on garbage (#1564).

The ``PreToolUse`` dispatch-quote gate had no off-switch: a false-positive on an
ordinary brief could wedge the fleet with no config escape. This adds
``[teatree] dispatch_quote_scan_enabled`` (default on). The distinctive
requirement is fail-LOUD on an unknown value: a mistyped ``"yes"``/``on``/``2``
must not silently fall back to the default (leaving the operator thinking the
gate is off) — it emits one loud stderr line and keeps the protective default.
"""

import json
import sqlite3
from pathlib import Path

import pytest

import hooks.scripts.hook_router as router


@pytest.fixture
def config_db(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """A config DB path the cold reader resolves; absent until a test seeds it.

    ``_dispatch_quote_scan_enabled`` reads the flag from the ``ConfigSetting``
    store via the Django-free ``cold_reader``, so pointing ``T3_CONFIG_DB`` at an
    unseeded path leaves the read failing open to the protective default.
    """
    db = tmp_path / "config.sqlite3"
    monkeypatch.setenv("T3_CONFIG_DB", str(db))
    monkeypatch.delenv("XDG_DATA_HOME", raising=False)
    return db


def _seed(db_path: Path, rows: dict[str, object]) -> None:
    conn = sqlite3.connect(str(db_path))
    try:
        conn.execute(
            "CREATE TABLE IF NOT EXISTS teatree_config_setting "
            "(id INTEGER PRIMARY KEY, scope TEXT NOT NULL DEFAULT '', key TEXT NOT NULL, value TEXT NOT NULL)"
        )
        for key, value in rows.items():
            conn.execute(
                "INSERT INTO teatree_config_setting (scope, key, value) VALUES ('', ?, ?)",
                (key, json.dumps(value)),
            )
        conn.commit()
    finally:
        conn.close()


class TestDispatchQuoteScanEnabledReader:
    def test_default_enabled_without_config(self, config_db: Path) -> None:
        assert router._dispatch_quote_scan_enabled() is True

    def test_bare_false_disables(self, config_db: Path) -> None:
        _seed(config_db, {"dispatch_quote_scan_enabled": False})
        assert router._dispatch_quote_scan_enabled() is False

    def test_bare_true_keeps_enabled(self, config_db: Path) -> None:
        _seed(config_db, {"dispatch_quote_scan_enabled": True})
        assert router._dispatch_quote_scan_enabled() is True

    def test_unknown_value_warns_loudly_and_keeps_default(self, config_db: Path, capsys) -> None:
        _seed(config_db, {"dispatch_quote_scan_enabled": "yes"})
        assert router._dispatch_quote_scan_enabled() is True, "an unknown value keeps the protective default"
        err = capsys.readouterr().err
        assert "dispatch_quote_scan_enabled" in err
        assert "not a boolean" in err.lower()

    def test_absent_value_is_silent(self, config_db: Path, capsys) -> None:
        _seed(config_db, {"some_other_flag": True})
        assert router._dispatch_quote_scan_enabled() is True
        assert capsys.readouterr().err == "", "an ABSENT setting is not unknown, so no warning"


class TestGateHonoursTheToggle:
    def test_disabled_skips_the_scan_entirely(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(router, "_dispatch_quote_scan_enabled", lambda: False)
        ran: list[dict] = []
        monkeypatch.setattr(router, "_run_dispatch_quote_scanner", lambda data: ran.append(data) or False)
        data = {"tool_name": "Task", "tool_input": {"prompt": "anything at all"}}
        assert router.handle_dispatch_prompt_quote_scanner(data) is False
        assert ran == [], "the scanner must not run when the gate is disabled"

    def test_enabled_runs_the_scan(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(router, "_dispatch_quote_scan_enabled", lambda: True)
        calls: list[dict] = []

        def _record(data: dict) -> bool:
            calls.append(data)
            return False

        monkeypatch.setattr(router, "_run_dispatch_quote_scanner", _record)
        data = {"tool_name": "Task", "tool_input": {"prompt": "anything at all"}}
        assert router.handle_dispatch_prompt_quote_scanner(data) is False
        assert calls == [data], "the scanner must run when the gate is enabled"
