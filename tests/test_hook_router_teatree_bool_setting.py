"""Tests for the shared ``_teatree_bool_setting``-backed ``<flag>`` readers (#1694).

``hook_router`` carried ~10 near-identical ``[teatree] <flag>`` boolean readers.
They are extracted to one ``_teatree_bool_setting(name, *, default)`` helper — the
DB-home ``teatree_settings`` adapter, which resolves a gate flag from the canonical
``ConfigSetting`` store via the Django-free ``cold_reader`` — and every reader
delegates to it.

Two behaviors each reader must preserve. A fail-OPEN reader (``default=True``)
returns ``True`` on a missing/unreadable store and on any value except a real DB
boolean ``false`` (a JSON string ``"false"`` does NOT disable). A fail-CLOSED
reader (``default=False``) returns ``False`` likewise unless a real DB boolean
``true`` is stored. The generic helper semantics are pinned by the config twin
``tests/config/test_teatree_settings_db_flip.py``; this file drives each router
reader end-to-end and proves the delegation.

The delegation assertions are anti-vacuous: monkeypatching the helper flips every
reader's output, which can only happen if the reader routes through it.
"""

import json
import sqlite3
from pathlib import Path

import pytest

import hooks.scripts.hook_router as router

# (reader function name, [teatree] key) — every [teatree]-table boolean flag reader.
_FAIL_OPEN_READERS: tuple[tuple[str, str], ...] = (
    ("_deny_circuit_breaker_enabled", "deny_circuit_breaker_enabled"),
    ("_skill_loading_gate_enabled", "skill_loading_gate_enabled"),
    ("_plan_edit_gate_enabled", "plan_edit_gate_enabled"),
    ("_mcp_privacy_gate_enabled", "mcp_privacy_gate_enabled"),
    ("_self_dm_gate_enabled", "self_dm_gate_enabled"),
    ("_orchestrator_bash_gate_enabled", "orchestrator_bash_gate_enabled"),
    # #1733: flipped to default-ON (fail-open) once the Agent matcher was wired.
    ("_orchestrator_boundary_agent_gate_enabled", "orchestrator_boundary_agent_gate_enabled"),
)

_FAIL_CLOSED_READERS: tuple[tuple[str, str], ...] = (
    ("_dispatch_quote_gate_on_task_create_enabled", "dispatch_quote_gate_on_task_create_enabled"),
)


@pytest.fixture
def config_db(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Route the cold reader at an isolated per-test DB path (absent until seeded)."""
    db = tmp_path / "db.sqlite3"
    monkeypatch.setenv("T3_CONFIG_DB", str(db))
    monkeypatch.delenv("XDG_DATA_HOME", raising=False)
    return db


def _seed_config_db(path: Path, rows: dict[str, object]) -> None:
    """Seed the DB-home ``teatree_config_setting`` store the flag readers resolve."""
    conn = sqlite3.connect(str(path))
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


class TestFailOpenReadersBehavior:
    @pytest.mark.parametrize(("reader", "key"), _FAIL_OPEN_READERS)
    def test_defaults_enabled_without_config(self, reader: str, key: str, config_db: Path) -> None:
        assert getattr(router, reader)() is True

    @pytest.mark.parametrize(("reader", "key"), _FAIL_OPEN_READERS)
    def test_bare_false_disables(self, reader: str, key: str, config_db: Path) -> None:
        _seed_config_db(config_db, {key: False})
        assert getattr(router, reader)() is False

    @pytest.mark.parametrize(("reader", "key"), _FAIL_OPEN_READERS)
    def test_quoted_false_does_not_disable(self, reader: str, key: str, config_db: Path) -> None:
        _seed_config_db(config_db, {key: "false"})
        assert getattr(router, reader)() is True


class TestFailClosedReadersBehavior:
    @pytest.mark.parametrize(("reader", "key"), _FAIL_CLOSED_READERS)
    def test_defaults_disabled_without_config(self, reader: str, key: str, config_db: Path) -> None:
        assert getattr(router, reader)() is False

    @pytest.mark.parametrize(("reader", "key"), _FAIL_CLOSED_READERS)
    def test_bare_true_enables(self, reader: str, key: str, config_db: Path) -> None:
        _seed_config_db(config_db, {key: True})
        assert getattr(router, reader)() is True

    @pytest.mark.parametrize(("reader", "key"), _FAIL_CLOSED_READERS)
    def test_quoted_true_does_not_enable(self, reader: str, key: str, config_db: Path) -> None:
        _seed_config_db(config_db, {key: "true"})
        assert getattr(router, reader)() is False


class TestReadersDelegateToHelper:
    """Patching the helper flips every reader's output.

    Anti-vacuous: that flip can only happen if the reader routes through
    ``_teatree_bool_setting``.
    """

    @pytest.mark.parametrize(("reader", "key"), _FAIL_OPEN_READERS)
    def test_fail_open_reader_routes_through_helper(
        self, reader: str, key: str, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        seen: list[tuple[str, bool]] = []

        def _fake(name: str, *, default: bool = True) -> bool:
            seen.append((name, default))
            return not default

        monkeypatch.setattr(router, "_teatree_bool_setting", _fake)
        # A fail-open reader defaults True, so the fake (which returns ``not
        # default``) forces it False — observable only through delegation.
        assert getattr(router, reader)() is False
        assert (key, True) in seen

    @pytest.mark.parametrize(("reader", "key"), _FAIL_CLOSED_READERS)
    def test_fail_closed_reader_routes_through_helper(
        self, reader: str, key: str, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        seen: list[tuple[str, bool]] = []

        def _fake(name: str, *, default: bool = True) -> bool:
            seen.append((name, default))
            return not default

        monkeypatch.setattr(router, "_teatree_bool_setting", _fake)
        # A fail-closed reader defaults False, so the fake forces it True.
        assert getattr(router, reader)() is True
        assert (key, False) in seen
