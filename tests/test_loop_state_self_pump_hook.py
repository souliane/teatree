"""The Stop self-pump honours the durable DB LoopState 'pause everything' (#1913).

The 2026-06-03 'pause everything' incident: there was no restart-surviving
paused state for the loop control plane. The DB ``LoopState`` tier (#1913) is the
single control plane — a ``LoopState`` row that pauses/disables the ``dispatch``
core loop (the loop the in-session Stop self-pump exists to drive) is the only
way to stop the pump's loop (loop control is ``/loops`` + the DB only; there is
no env kill-switch).

When that durable state says PAUSED or DISABLED the self-pump must suppress (a
clean no-op) so a paused loop stays paused across a session restart. An ENABLED /
absent state leaves the self-pump behaviour unchanged (no regression), and an
unreadable control plane fails OPEN (the availability/ownership gates still
decide) so the Stop hook can never crash.

fast-hooks changed the READ MECHANISM again: the bare-``python3`` Stop hook
cannot ``django.setup()`` and no longer shells out to ``t3 loop loop-state`` (that
child booted Django, ~3s per Stop — the recurring TIMEOUT). It now reads the
``teatree_loop_state`` row DIRECTLY via the Django-free
``teatree.config.cold_reader.loop_status``. So these tests build a REAL
``teatree_loop_state`` sqlite DB and point ``cold_reader`` at it via
``T3_CONFIG_DB`` (no mocks — the fail-open and status semantics run against real
sqlite), rather than stubbing a subprocess.
"""

import os
import sqlite3
import time
from pathlib import Path

import pytest

import hooks.scripts.hook_router as router
from hooks.scripts.hook_router import _OWNER_LOOP, _write_loop_registry, handle_loop_self_pump


@pytest.fixture(autouse=True)
def _isolation(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    state = tmp_path / "state"
    state.mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr(router, "STATE_DIR", state)
    monkeypatch.setenv("T3_LOOP_REGISTRY_DIR", str(tmp_path / "data"))
    (tmp_path / "data").mkdir(parents=True, exist_ok=True)
    monkeypatch.setenv("TEATREE_BASH_ENV_FILE", str(tmp_path / "no-bash-env"))
    # A live user pause (away mode) is its own suppression path; pin present so
    # only the DB LoopState decides here.
    monkeypatch.setattr(router, "_pause_suppresses_self_pump", lambda: False)


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


def _loop_state_db(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    *,
    dispatch_status: str | None = None,
    other_loop: tuple[str, str] | None = None,
) -> None:
    """Build a real ``teatree_loop_state`` DB and point ``cold_reader`` at it.

    *dispatch_status* seeds the ``dispatch`` row (``enabled`` / ``paused`` /
    ``disabled``); ``None`` leaves the table with no ``dispatch`` row (the
    absent-row → ``enabled`` fall-through). *other_loop* optionally seeds an
    unrelated ``(name, status)`` row to prove the gate keys only on ``dispatch``.
    ``T3_CONFIG_DB`` makes ``cold_reader.canonical_config_db`` resolve this DB.
    """
    db = tmp_path / "loopstate.sqlite3"
    conn = sqlite3.connect(db)
    try:
        conn.execute(
            "CREATE TABLE teatree_loop_state ("
            "id INTEGER PRIMARY KEY, name TEXT NOT NULL UNIQUE, "
            "status TEXT NOT NULL, created_at TEXT, updated_at TEXT)"
        )
        rows = []
        if dispatch_status is not None:
            rows.append(("dispatch", dispatch_status))
        if other_loop is not None:
            rows.append(other_loop)
        conn.executemany("INSERT INTO teatree_loop_state (name, status) VALUES (?, ?)", rows)
        conn.commit()
    finally:
        conn.close()
    monkeypatch.setenv("T3_CONFIG_DB", str(db))


class TestSelfPumpHonoursDbLoopState:
    def test_db_paused_dispatch_loop_makes_owner_stop_hook_a_noop(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _loop_state_db(tmp_path, monkeypatch, dispatch_status="paused")
        _own_loop("owner-1")
        _fake_pending(monkeypatch, [{"task_id": 4, "subagent": "x", "phase": "coding", "issue_url": "u"}])

        result = handle_loop_self_pump({"session_id": "owner-1"})

        assert result is not True  # paused: no block, the session may end

    def test_db_disabled_dispatch_loop_makes_owner_stop_hook_a_noop(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _loop_state_db(tmp_path, monkeypatch, dispatch_status="disabled")
        _own_loop("owner-1")
        _fake_pending(monkeypatch, [{"task_id": 4, "subagent": "x", "phase": "coding", "issue_url": "u"}])

        result = handle_loop_self_pump({"session_id": "owner-1"})

        assert result is not True

    def test_db_paused_dispatch_loop_does_not_probe_pending_work(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _loop_state_db(tmp_path, monkeypatch, dispatch_status="paused")
        _own_loop("owner-1")
        probed = {"called": False}

        def _spy() -> list[dict]:
            probed["called"] = True
            return [{"task_id": 1, "subagent": "x", "phase": "c", "issue_url": "u"}]

        monkeypatch.setattr(router, "_consolidated_pending_work", _spy)

        result = handle_loop_self_pump({"session_id": "owner-1"})

        assert probed["called"] is False  # gate checked BEFORE the pending probe
        assert result is not True

    def test_absent_row_leaves_owner_pumping(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        # No durable ``dispatch`` row → cold_reader resolves the runnable
        # ``enabled`` default → no regression: the owner with pending work pumps.
        _loop_state_db(tmp_path, monkeypatch, dispatch_status=None)
        _own_loop("owner-1")
        _fake_pending(monkeypatch, [{"task_id": 4, "subagent": "x", "phase": "coding", "issue_url": "u"}])

        result = handle_loop_self_pump({"session_id": "owner-1"})

        assert result is True

    def test_db_enabled_dispatch_loop_leaves_owner_pumping(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _loop_state_db(tmp_path, monkeypatch, dispatch_status="enabled")
        _own_loop("owner-1")
        _fake_pending(monkeypatch, [{"task_id": 4, "subagent": "x", "phase": "coding", "issue_url": "u"}])

        result = handle_loop_self_pump({"session_id": "owner-1"})

        assert result is True

    def test_paused_other_loop_does_not_suppress_the_pump(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # The self-pump drives the always-on ``dispatch`` loop; the gate keys
        # ONLY on ``dispatch`` — a paused UNRELATED loop is invisible to it, so
        # the pump still fires. Anti-vacuous: a real ``paused`` row exists, just
        # for a different loop.
        _loop_state_db(tmp_path, monkeypatch, dispatch_status="enabled", other_loop=("review", "paused"))
        _own_loop("owner-1")
        _fake_pending(monkeypatch, [{"task_id": 4, "subagent": "x", "phase": "coding", "issue_url": "u"}])

        result = handle_loop_self_pump({"session_id": "owner-1"})

        assert result is True

    def test_missing_db_fails_open_pump_proceeds(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        # A Stop hook must be crash-proof: an unreadable control plane (the DB
        # file does not exist) makes cold_reader fail OPEN to the runnable
        # default, so the gate defers to env/availability/ownership and the pump
        # runs.
        monkeypatch.setenv("T3_CONFIG_DB", str(tmp_path / "does-not-exist.sqlite3"))
        _own_loop("owner-1")
        _fake_pending(monkeypatch, [{"task_id": 4, "subagent": "x", "phase": "coding", "issue_url": "u"}])

        result = handle_loop_self_pump({"session_id": "owner-1"})

        assert result is True

    def test_absent_table_fails_open_pump_proceeds(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        # A fresh DB with no ``teatree_loop_state`` table (pre-migration cold
        # state) also fails OPEN to the runnable default.
        db = tmp_path / "fresh.sqlite3"
        sqlite3.connect(db).close()
        monkeypatch.setenv("T3_CONFIG_DB", str(db))
        _own_loop("owner-1")
        _fake_pending(monkeypatch, [{"task_id": 4, "subagent": "x", "phase": "coding", "issue_url": "u"}])

        result = handle_loop_self_pump({"session_id": "owner-1"})

        assert result is True
