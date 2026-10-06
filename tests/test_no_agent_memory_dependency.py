"""teatree resolves all required state from its own stores — never assistant memory (#3277).

The invariant: teatree's runtime (worker, loops, CLI, gates) must resolve every
piece of state it needs to *function* — config, gate enablement, publishing
doctrine, factory settings, loop state — from teatree's OWN stores (the DB-home
``ConfigSetting`` / ``LoopState`` tables, ``pass``, repo config). An assistant's
``MEMORY.md`` / ``~/.claude/**/memory`` is a convenience for the assistant, NEVER a
functional input to the factory. Were it otherwise, the factory would behave
differently on another machine / another agent / a fresh session.

These tests pin the invariant behaviorally: a representative slice of the runtime
state resolvers is sampled twice — once with the assistant-memory dir absent, once
with it POPULATED by config-shaped bait whose values contradict the DB — and the two
snapshots must be byte-identical (and equal to the DB-seeded values).
"""

# test-path: cross-cutting — a multi-package invariant spanning cli, config, and hooks.

import json
import sqlite3
from pathlib import Path

import pytest

from teatree.cli import teatree_gate
from teatree.config import cold_reader

# Config-shaped bait written into the assistant memory. Every value CONTRADICTS the
# DB seed below, so if any resolver ever read memory as a functional input the
# snapshot would diverge from the DB-seeded truth and the equality assertion fails.
_MEMORY_BAIT = (
    "# Memory index\n"
    "\n"
    "mode = interactive\n"
    "orchestrator_bash_gate_enabled = false\n"
    "require_human_approval_to_merge = true\n"
    "the dream loop status is paused; the factory must not merge unattended.\n"
)

# The DB seed — deliberately non-default so a snapshot that matched the per-setting
# defaults (rather than the store) would also be caught.
_DB_SETTINGS: dict[str, object] = {
    "mode": "auto",
    "orchestrator_bash_gate_enabled": True,
    "require_human_approval_to_merge": False,
}


def _seed_config_db(db_path: Path) -> None:
    """Create the DB-home store with the table the cold readers query, and seed it."""
    conn = sqlite3.connect(str(db_path))
    try:
        conn.execute(
            "CREATE TABLE teatree_config_setting "
            "(id INTEGER PRIMARY KEY, scope TEXT NOT NULL DEFAULT '', key TEXT NOT NULL, "
            "value TEXT NOT NULL, created_at TEXT NOT NULL, updated_at TEXT NOT NULL, "
            "UNIQUE(scope, key))"
        )
        for key, value in _DB_SETTINGS.items():
            conn.execute(
                "INSERT INTO teatree_config_setting (scope, key, value, created_at, updated_at) "
                "VALUES ('', ?, ?, '2026-01-01 00:00:00', '2026-01-01 00:00:00')",
                (key, json.dumps(value)),
            )
        conn.commit()
    finally:
        conn.close()


def _runtime_state_snapshot() -> dict[str, object]:
    """A representative slice of the runtime state teatree needs to function.

    Every entry resolves through teatree's own DB-home store (or a per-setting
    default) — the worker/loop publishing doctrine, gate enablement across three
    reader surfaces (the cold CLI reader and the ``teatree_gate`` CLI helper), and a
    factory merge-approval setting.
    """
    return {
        "mode": cold_reader.str_setting("mode", default="interactive"),
        "bash_gate_cold": cold_reader.bool_setting("orchestrator_bash_gate_enabled", default=True),
        "bash_gate_cli": teatree_gate.gate_is_enabled(),
        "require_human_approval_to_merge": cold_reader.bool_setting("require_human_approval_to_merge", default=True),
    }


@pytest.fixture
def seeded_db(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    db_path = tmp_path / "db.sqlite3"
    _seed_config_db(db_path)
    monkeypatch.setenv("T3_CONFIG_DB", str(db_path))
    return db_path


def _point_home_at(monkeypatch: pytest.MonkeyPatch, home: Path) -> None:
    """Make ``Path.home()`` (and the memory-dir resolvers built on it) resolve under *home*."""
    home.mkdir(parents=True, exist_ok=True)
    monkeypatch.setenv("HOME", str(home))


def _populate_assistant_memory(home: Path) -> None:
    """Write a populated assistant memory tree with config-shaped bait under *home*."""
    memory_dir = home / ".claude" / "projects" / "-home-teatree-project" / "memory"
    memory_dir.mkdir(parents=True)
    (memory_dir / "MEMORY.md").write_text(_MEMORY_BAIT, encoding="utf-8")
    (memory_dir / "factory-config.md").write_text(_MEMORY_BAIT, encoding="utf-8")


def test_runtime_state_identical_with_memory_absent_and_populated(
    seeded_db: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    absent_home = tmp_path / "home-absent"
    _point_home_at(monkeypatch, absent_home)
    assert not (absent_home / ".claude").exists()
    snapshot_absent = _runtime_state_snapshot()

    populated_home = tmp_path / "home-populated"
    _point_home_at(monkeypatch, populated_home)
    _populate_assistant_memory(populated_home)
    snapshot_populated = _runtime_state_snapshot()

    # The factory resolves identically whether the assistant memory is absent or
    # populated with contradicting bait: memory is never a functional input.
    assert snapshot_absent == snapshot_populated
    # ...and the resolved values are the DB-seeded truth, not the memory bait.
    assert snapshot_absent == {
        "mode": "auto",
        "bash_gate_cold": True,
        "bash_gate_cli": True,
        "require_human_approval_to_merge": False,
    }


def test_no_state_resolver_reads_the_home_tree(seeded_db: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A poisoned ``Path.home`` proves the state resolvers never touch the home tree.

    ``seeded_db`` sets ``T3_CONFIG_DB``, so the store is resolved WITHOUT ``Path.home()``
    (its only legitimate use here). Any remaining ``Path.home()`` call therefore means a
    resolver reached for the assistant's home tree — where ``~/.claude/.../memory`` lives —
    and the poison raises. A clean snapshot proves state resolution is fully decoupled from it.
    """

    def _poisoned_home(cls: type[Path]) -> Path:
        message = "a runtime state resolver read Path.home() — assistant memory must not be an input"
        raise AssertionError(message)

    monkeypatch.setattr(Path, "home", classmethod(_poisoned_home))
    assert _runtime_state_snapshot()  # completes without touching Path.home()
