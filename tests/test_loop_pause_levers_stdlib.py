"""The Stop self-pump pause levers read durable state WITHOUT django.setup() (#2559, fast-hooks).

The Stop hook is invoked as a bare ``python3`` (``hooks.json``): the harness
never sources the user's shell profile and the interpreter is whatever the
harness picks — it has NO ``uv`` env, so an in-process ``django.setup()`` cannot
be relied on. Before #2559 both durable pause levers gated their read on that
bootstrap and FAILED OPEN when it failed — a durable DB pause / away override was
silently ineffective at suppressing the self-pump.

#2559 fixed that by shelling out to the ``t3`` CLI (a child process that
bootstraps Django). fast-hooks removes even that: the ``t3`` child cold-booted
Django (~3s), which dominated the ~15s Stop hook and blew the 30s timeout (the
recurring TIMEOUT). The lever now reads durable state DIRECTLY in stdlib:
``db_loop_state_suppresses_self_pump`` reads the ``teatree_loop_state`` row via the
Django-free ``teatree.config.cold_reader.loop_status``. (#4202 retired the second,
mode-posture lever: a mode is a pure loop table, so the brake on a self-waking
directive is now the mask over the self-pump's own loop — see
``tests/teatree_loop/test_standing_directives.py``.)

These tests reproduce the bare-``python3`` context (the in-process bootstrap is
forced to fail) and prove a durable pause STILL suppresses the pump — now with no
``django.setup()`` AND no per-lever subprocess. The #2559 structural invariant (no
in-process django bootstrap in the lever) is re-pinned against the new mechanism.
"""

import os
import sqlite3
import time
from pathlib import Path

import pytest

import hooks.scripts.hook_router as router
import hooks.scripts.loop_state_self_pump_gate as gate
from hooks.scripts.hook_router import _OWNER_LOOP, _write_loop_registry, handle_loop_self_pump


@pytest.fixture(autouse=True)
def _bare_python3_stop_context(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Reproduce the real bare-``python3`` Stop hook context, fully isolated.

    The in-process django bootstrap is forced to fail (as in production), and the
    state dir / registry / bash-env are redirected into ``tmp_path`` so a
    developer's real config never leaks into the test. With no schedule row
    seeded, the away probe sees no schedule windows and never falls back to the
    ``t3`` subprocess unless a test configures one.
    """
    state = tmp_path / "state"
    state.mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr(router, "STATE_DIR", state)
    monkeypatch.setenv("T3_LOOP_REGISTRY_DIR", str(tmp_path / "data"))
    (tmp_path / "data").mkdir(parents=True, exist_ok=True)
    monkeypatch.setenv("TEATREE_BASH_ENV_FILE", str(tmp_path / "no-bash-env"))
    # The router still imports the in-process bootstrap for OTHER handlers; force
    # it False to prove the levers do NOT depend on an in-process django.setup().
    monkeypatch.setattr(router, "bootstrap_teatree_django", lambda: False)


def _own_loop(session_id: str) -> None:
    _write_loop_registry(
        {
            _OWNER_LOOP: {
                "session_id": session_id,
                "agent_id": "a",
                "pid": os.getpid(),
                "heartbeat_ts": int(time.time()),
            }
        }
    )


def _fake_pending(monkeypatch: pytest.MonkeyPatch, entries: list[dict]) -> None:
    monkeypatch.setattr(router, "_consolidated_pending_work", lambda: entries)


def _config_db(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, *, dispatch_status: str | None = None) -> Path:
    """Build the PRIMARY DB (with a ``teatree_loop_state`` table) and point cold_reader at it.

    ``T3_CONFIG_DB`` makes ``cold_reader.canonical_config_db`` resolve this DB.
    *dispatch_status* seeds the ``dispatch`` row; ``None`` leaves it absent (the
    ``enabled`` fall-through).
    """
    db = tmp_path / "db.sqlite3"
    conn = sqlite3.connect(db)
    try:
        conn.executescript(
            "CREATE TABLE teatree_loop_state ("
            "id INTEGER PRIMARY KEY, name TEXT NOT NULL UNIQUE, "
            "status TEXT NOT NULL, created_at TEXT, updated_at TEXT);"
        )
        if dispatch_status is not None:
            conn.execute("INSERT INTO teatree_loop_state (name, status) VALUES ('dispatch', ?)", (dispatch_status,))
        conn.commit()
    finally:
        conn.close()
    monkeypatch.setenv("T3_CONFIG_DB", str(db))
    return db


class TestDbPauseLeverReadsViaColdReader:
    """``db_loop_state_suppresses_self_pump`` reads the durable pause via ``cold_reader``."""

    def test_lever_never_imports_in_process_django_bootstrap(self) -> None:
        # The structural #2559 invariant, re-pinned: the lever must NOT import the
        # in-process django bootstrap — it is Django-free (cold_reader + a src
        # bootstrap) so the bare-python3 Stop hook never needs its own
        # ``django.setup()``. A re-introduced bootstrap would silently reinstate
        # the fail-open bug. It also no longer shells out per-lever (fast-hooks).
        assert not hasattr(gate, "bootstrap_teatree_django")
        assert not hasattr(gate, "subprocess")
        assert hasattr(gate, "teatree_src_on_path")

    def test_db_paused_dispatch_suppresses_under_bare_python3(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # The exact #2559 reproduction: in-process django.setup() is impossible,
        # yet a durable PAUSE on ``dispatch`` is readable via cold_reader.
        _config_db(tmp_path, monkeypatch, dispatch_status="paused")
        assert gate.db_loop_state_suppresses_self_pump() is True

    def test_db_disabled_dispatch_suppresses_under_bare_python3(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _config_db(tmp_path, monkeypatch, dispatch_status="disabled")
        assert gate.db_loop_state_suppresses_self_pump() is True

    def test_db_enabled_dispatch_does_not_suppress(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        _config_db(tmp_path, monkeypatch, dispatch_status="enabled")
        assert gate.db_loop_state_suppresses_self_pump() is False

    def test_absent_row_fails_open(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        _config_db(tmp_path, monkeypatch, dispatch_status=None)
        assert gate.db_loop_state_suppresses_self_pump() is False

    def test_missing_db_fails_open(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("T3_CONFIG_DB", str(tmp_path / "nope.sqlite3"))
        assert gate.db_loop_state_suppresses_self_pump() is False


class TestStopSelfPumpEndToEndUnderBarePython3:
    """The whole handler suppresses through a durable pause in the bare context."""

    def test_db_pause_suppresses_pump_even_with_pending_work(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _config_db(tmp_path, monkeypatch, dispatch_status="paused")
        _own_loop("owner-1")
        _fake_pending(monkeypatch, [{"task_id": 4, "subagent": "x", "phase": "coding", "issue_url": "u"}])

        result = handle_loop_self_pump({"session_id": "owner-1"})

        assert result is not True  # paused: no block, the session may end
