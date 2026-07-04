"""hooks.scripts.worker_supervisor — SessionStart worker resurrection (#1796).

The decision logic is tested with injected collaborators (no real Django, flock, or
subprocess): spawn only when enabled AND the flock is free, and fail-open to a no-op
on any error. ``main`` never raises into the SessionStart hook.
"""

import json
import sqlite3
import sys
from pathlib import Path
from unittest.mock import patch

import pytest

_HOOKS_DIR = Path(__file__).resolve().parents[2] / "hooks" / "scripts"
if str(_HOOKS_DIR) not in sys.path:
    sys.path.insert(0, str(_HOOKS_DIR))

import django  # noqa: E402
import django_bootstrap  # noqa: E402
import worker_supervisor as supervisor  # noqa: E402


class _Spy:
    def __init__(self) -> None:
        self.calls = 0

    def __call__(self) -> None:
        self.calls += 1


def test_spawns_when_enabled_and_flock_free() -> None:
    spawn = _Spy()
    action = supervisor.resurrect_worker(enabled=lambda: True, flock_free=lambda: True, spawn=spawn)
    assert action == "spawned"
    assert spawn.calls == 1


def test_no_spawn_when_disabled() -> None:
    spawn = _Spy()
    action = supervisor.resurrect_worker(enabled=lambda: False, flock_free=lambda: True, spawn=spawn)
    assert action == "disabled"
    assert spawn.calls == 0


def test_no_spawn_when_flock_held() -> None:
    spawn = _Spy()
    action = supervisor.resurrect_worker(enabled=lambda: True, flock_free=lambda: False, spawn=spawn)
    assert action == "already-running"
    assert spawn.calls == 0


def test_fails_open_when_spawn_raises() -> None:
    def boom() -> None:
        msg = "no t3 on PATH"
        raise OSError(msg)

    action = supervisor.resurrect_worker(enabled=lambda: True, flock_free=lambda: True, spawn=boom)
    assert action == "error"


def test_main_drains_stdin_and_never_raises(monkeypatch) -> None:
    monkeypatch.setattr(sys, "argv", ["worker_supervisor.py", "--event", "SessionStart"])
    monkeypatch.setattr(sys, "stdin", __import__("io").StringIO('{"session_id": "s1"}'))
    with patch.object(supervisor, "resurrect_worker", return_value="disabled") as resurrect:
        assert supervisor.main() == 0
    resurrect.assert_called_once_with()


def _config_db(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, *, rows: list[tuple[str, str, object]]) -> None:
    """Build a PRIMARY config DB with the ``teatree_config_setting`` table + point cold_reader at it."""
    db = tmp_path / "db.sqlite3"
    conn = sqlite3.connect(db)
    try:
        conn.execute(
            "CREATE TABLE teatree_config_setting ("
            "id INTEGER PRIMARY KEY, scope TEXT, key TEXT, value TEXT, created_at TEXT, updated_at TEXT)"
        )
        for scope, key, value in rows:
            conn.execute(
                "INSERT INTO teatree_config_setting (scope, key, value) VALUES (?, ?, ?)",
                (scope, key, json.dumps(value)),
            )
        conn.commit()
    finally:
        conn.close()
    monkeypatch.setenv("T3_CONFIG_DB", str(db))


def _arm_django_boot_spy(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    """Record every Django boot the enable check might trigger; ``[] == zero boots`` (#2879)."""
    boots: list[str] = []
    monkeypatch.setattr(django, "setup", lambda *_a, **_k: boots.append("boot"))
    # The supervisor reads the flag Django-free; this spy pins that — a
    # re-introduced ``bootstrap_teatree_django()`` would fire it.
    monkeypatch.setattr(django_bootstrap, "bootstrap_teatree_django", lambda: boots.append("boot") or True)
    return boots


class TestWorkerEnabledColdRead:
    """``_worker_enabled`` reads the DB-home flag Django-FREE — zero ``django.setup()`` (#2879)."""

    def _clear_env(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("T3_LOOP_RUNNER_ENABLED", raising=False)
        monkeypatch.delenv("T3_OVERLAY_NAME", raising=False)

    def test_flag_off_returns_false_and_boots_no_django(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        self._clear_env(monkeypatch)
        _config_db(tmp_path, monkeypatch, rows=[])  # no loop_runner_enabled row -> default OFF
        boots = _arm_django_boot_spy(monkeypatch)
        assert supervisor._worker_enabled() is False
        assert boots == []  # the default-OFF flag read pays NO django.setup() (#2879 parity)

    def test_global_db_row_true_enables_via_cold_read(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        self._clear_env(monkeypatch)
        _config_db(tmp_path, monkeypatch, rows=[("", "loop_runner_enabled", True)])
        boots = _arm_django_boot_spy(monkeypatch)
        assert supervisor._worker_enabled() is True
        assert boots == []  # resolving ON still boots no Django

    def test_overlay_scope_row_wins_over_global(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        self._clear_env(monkeypatch)
        monkeypatch.setenv("T3_OVERLAY_NAME", "dogfood")
        # global OFF, but the active overlay opted IN.
        _config_db(
            tmp_path,
            monkeypatch,
            rows=[("", "loop_runner_enabled", False), ("dogfood", "loop_runner_enabled", True)],
        )
        boots = _arm_django_boot_spy(monkeypatch)
        assert supervisor._worker_enabled() is True
        assert boots == []

    def test_env_var_enables_without_touching_the_db(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("T3_LOOP_RUNNER_ENABLED", "1")
        monkeypatch.setenv("T3_CONFIG_DB", "/nonexistent/should-not-be-read.sqlite3")
        boots = _arm_django_boot_spy(monkeypatch)
        assert supervisor._worker_enabled() is True
        assert boots == []

    def test_unreadable_db_fails_off(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        self._clear_env(monkeypatch)
        monkeypatch.setenv("T3_CONFIG_DB", str(tmp_path / "absent.sqlite3"))
        boots = _arm_django_boot_spy(monkeypatch)
        assert supervisor._worker_enabled() is False  # fail-open to OFF, never a crash
        assert boots == []
