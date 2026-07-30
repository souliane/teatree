# test-path: cross-cutting
"""Pin the worker's two ``loop_runner_enabled`` defaults together (PR-28, Fable bug #1).

A fresh install has no explicit ``ConfigSetting`` row, so whether SessionStart spawns
a worker is decided by ``hooks/scripts/worker_supervisor.py``'s Django-free cold read
— NOT by the ``UserSettings.loop_runner_enabled`` dataclass default the hot path uses.
The two are independent literals in different modules; flipping only one silently
leaves a default-ON install with a worker that never spawns (or a default-OFF one that
spawns anyway). This conformance test makes that drift class die structurally: the
cold-read default constant and the dataclass default must agree, AND the cold reader
must actually resolve to the dataclass default when no row and no env override exist.

Integration-first: the behavioural arm exercises the real ``teatree.config.cold_reader``
stdlib-sqlite path against an empty config DB, exactly as the hook does at SessionStart.
"""

import sqlite3
import sys
from pathlib import Path

import pytest

from teatree.config import UserSettings

_HOOKS_DIR = Path(__file__).resolve().parents[2] / "hooks" / "scripts"
if str(_HOOKS_DIR) not in sys.path:
    sys.path.insert(0, str(_HOOKS_DIR))

import worker_supervisor as supervisor  # noqa: E402 — after the sys.path bootstrap above


def _empty_config_db(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A config DB with the settings table but NO rows, pointed at by the cold reader."""
    db = tmp_path / "db.sqlite3"
    conn = sqlite3.connect(db)
    try:
        conn.execute(
            "CREATE TABLE teatree_config_setting ("
            "id INTEGER PRIMARY KEY, scope TEXT, key TEXT, value TEXT, created_at TEXT, updated_at TEXT)"
        )
        conn.commit()
    finally:
        conn.close()
    monkeypatch.setenv("T3_CONFIG_DB", str(db))
    monkeypatch.delenv("T3_LOOP_RUNNER_ENABLED", raising=False)
    monkeypatch.delenv("T3_OVERLAY_NAME", raising=False)


def test_cold_read_default_constant_equals_dataclass_default() -> None:
    # Structural parity: the two independent literals must be the same value.
    assert UserSettings().loop_runner_enabled == supervisor._ENABLED_DEFAULT


def test_cold_read_resolves_to_dataclass_default_with_no_row(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    # Behavioural parity: with no env override and no ConfigSetting row (a fresh
    # install), the real cold read the hook uses returns the dataclass default — so a
    # fresh box's worker-spawn decision matches the hot path exactly.
    _empty_config_db(tmp_path, monkeypatch)
    assert supervisor._worker_enabled() is UserSettings().loop_runner_enabled


def test_flipping_only_one_default_would_break_parity() -> None:
    # The anti-vacuity anchor: had PR-28 flipped only the dataclass to True while the
    # cold-read constant stayed False (the exact Fable-#1 drift), this equality fails.
    dataclass_default = UserSettings().loop_runner_enabled
    cold_read_default = supervisor._ENABLED_DEFAULT
    assert dataclass_default is True  # PR-28 flipped it ON
    assert cold_read_default is dataclass_default
