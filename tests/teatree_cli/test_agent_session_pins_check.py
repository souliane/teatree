"""``_check_agent_session_pins`` — the `t3 doctor` agent-config gate (teatree#2216).

Validates the agent model + effort settings: a bad ``agent_session_effort`` (off
the strict CLI scale) is a hard FAIL surfaced loudly (the parser raises); an
unrecognised model in ``agent_session_model`` or an ``agent_skill_models`` floor
is a WARN (it ranks most-capable, so not fatal, but likely a typo). An absent or
all-valid config is silently OK. The settings are DB-home, read via the pre-Django
``cold_reader``, so tests seed a cold-readable DB and point ``T3_CONFIG_DB`` at it.
"""

import json
import sqlite3
from pathlib import Path

import pytest

from teatree.cli._doctor_checks import _check_agent_session_pins


def _seed(db: Path, **settings: object) -> None:
    conn = sqlite3.connect(str(db))
    try:
        conn.execute(
            "CREATE TABLE IF NOT EXISTS teatree_config_setting ("
            "id INTEGER PRIMARY KEY, scope TEXT NOT NULL DEFAULT '', "
            "key TEXT NOT NULL, value TEXT NOT NULL)"
        )
        for key, value in settings.items():
            conn.execute(
                "INSERT INTO teatree_config_setting (scope, key, value) VALUES ('', ?, ?)",
                (key, json.dumps(value)),
            )
        conn.commit()
    finally:
        conn.close()


def _point_at(monkeypatch: pytest.MonkeyPatch, db: Path) -> None:
    monkeypatch.setenv("T3_CONFIG_DB", str(db))


class TestAgentSessionPinsCheck:
    def test_absent_config_is_ok_silent(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        _point_at(monkeypatch, tmp_path / "absent.sqlite3")
        assert _check_agent_session_pins() is True
        assert capsys.readouterr().out == ""

    def test_all_valid_is_ok_silent(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        db = tmp_path / "config.sqlite3"
        _seed(
            db,
            agent_session_model="opus",
            agent_session_effort="xhigh",
            agent_skill_models={"code-review": "opus"},
        )
        _point_at(monkeypatch, db)
        assert _check_agent_session_pins() is True
        assert capsys.readouterr().out == ""

    def test_bad_effort_is_hard_fail(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        db = tmp_path / "config.sqlite3"
        _seed(db, agent_session_effort="off")
        _point_at(monkeypatch, db)
        assert _check_agent_session_pins() is False
        out = capsys.readouterr().out
        assert "FAIL" in out
        assert "session_effort" in out
        assert "off" in out

    def test_ultracode_effort_is_hard_fail(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        # "ultracode" is a session/settings concept, never an effort scale value.
        db = tmp_path / "config.sqlite3"
        _seed(db, agent_session_effort="ultracode")
        _point_at(monkeypatch, db)
        assert _check_agent_session_pins() is False
        assert "FAIL" in capsys.readouterr().out

    def test_unknown_session_model_warns(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        db = tmp_path / "config.sqlite3"
        _seed(db, agent_session_model="gpt-9")
        _point_at(monkeypatch, db)
        assert _check_agent_session_pins() is True
        out = capsys.readouterr().out
        assert "WARN" in out
        assert "gpt-9" in out

    def test_unknown_skill_floor_model_warns(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        # A floor naming no known tier substring — a real typo, no substring match.
        db = tmp_path / "config.sqlite3"
        _seed(db, agent_skill_models={"code-review": "opsu"})
        _point_at(monkeypatch, db)
        assert _check_agent_session_pins() is True
        out = capsys.readouterr().out
        assert "WARN" in out
        assert "opsu" in out
        assert "code-review" in out

    def test_tier_substring_superstring_is_not_warned(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        # A superstring that contains a known tier (e.g. a dated id) is fine —
        # the system resolves it to that tier by substring, so no false typo WARN.
        db = tmp_path / "config.sqlite3"
        _seed(db, agent_skill_models={"c": "sonnet-4-6"})
        _point_at(monkeypatch, db)
        assert _check_agent_session_pins() is True
        assert capsys.readouterr().out == ""

    def test_known_tiers_do_not_warn(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        db = tmp_path / "config.sqlite3"
        _seed(db, agent_skill_models={"a": "haiku", "b": "sonnet", "c": "opus"})
        _point_at(monkeypatch, db)
        assert _check_agent_session_pins() is True
        assert capsys.readouterr().out == ""

    def test_dated_full_id_does_not_warn(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        # A dated full id whose tier substring is recognised is fine.
        db = tmp_path / "config.sqlite3"
        _seed(db, agent_session_model="claude-opus-4-9")
        _point_at(monkeypatch, db)
        assert _check_agent_session_pins() is True
        assert capsys.readouterr().out == ""
