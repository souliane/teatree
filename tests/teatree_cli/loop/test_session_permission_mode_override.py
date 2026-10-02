"""The operator's escape hatch for the pinned loop-session permission mode (#3528).

``t3 loop start`` pinned ``--permission-mode`` unconditionally with no way to
override it, so an operator who wants the loop session on a narrower mode had to
edit teatree. The pin stays the DEFAULT — it is what makes the doctor's ``auto``
advice safe — but it is a setting now, not a constant.

Integration-first per the Test-Writing Doctrine: real rows in a real config DB,
read back through the production cold reader the CLI itself uses.
"""

import json
import sqlite3
from pathlib import Path

import pytest

from teatree.agents import permission_modes
from teatree.cli.loop.app import _session_pin_flags

_SETTING = "agent_session_permission_mode"
_ENV = "T3_AGENT_SESSION_PERMISSION_MODE"


def _seeded_db(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, **rows: object) -> None:
    db = tmp_path / "db.sqlite3"
    conn = sqlite3.connect(str(db))
    conn.execute(
        "CREATE TABLE IF NOT EXISTS teatree_config_setting (id INTEGER PRIMARY KEY, scope TEXT, key TEXT, value TEXT)"
    )
    for key, value in rows.items():
        conn.execute(
            "INSERT INTO teatree_config_setting (scope, key, value) VALUES ('', ?, ?)", (key, json.dumps(value))
        )
    conn.commit()
    conn.close()
    monkeypatch.setenv("T3_CONFIG_DB", str(db))


def _mode_of(flags: list[str]) -> str:
    return flags[flags.index("--permission-mode") + 1]


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("T3_OVERLAY_NAME", raising=False)
    monkeypatch.delenv(_ENV, raising=False)


class TestSessionPermissionModeOverride:
    def test_default_keeps_the_unattended_pin(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        _seeded_db(tmp_path, monkeypatch)
        assert _mode_of(_session_pin_flags()) == permission_modes.UNATTENDED

    def test_configured_mode_overrides_the_pin(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        _seeded_db(tmp_path, monkeypatch, **{_SETTING: permission_modes.READER_DEFAULT_DENY})
        assert _mode_of(_session_pin_flags()) == permission_modes.READER_DEFAULT_DENY

    def test_the_flag_is_still_emitted_exactly_once(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        _seeded_db(tmp_path, monkeypatch, **{_SETTING: permission_modes.READER_DEFAULT_DENY})
        assert _session_pin_flags().count("--permission-mode") == 1

    def test_a_blank_stored_value_is_no_opinion(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        _seeded_db(tmp_path, monkeypatch, **{_SETTING: "   "})
        assert _mode_of(_session_pin_flags()) == permission_modes.UNATTENDED

    def test_env_overrides_the_stored_setting(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        _seeded_db(tmp_path, monkeypatch, **{_SETTING: permission_modes.READER_DEFAULT_DENY})
        monkeypatch.setenv(_ENV, permission_modes.UNATTENDED)
        assert _mode_of(_session_pin_flags()) == permission_modes.UNATTENDED
