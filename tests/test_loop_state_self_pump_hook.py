"""The Stop self-pump honours the durable DB LoopState 'pause everything' (#1913).

The 2026-06-03 'pause everything' incident: there was no restart-surviving
paused state for the loop control plane. ``T3_LOOPS_DISABLED=all`` is an env
kill-switch (process / file scoped); #1913 adds a DURABLE DB equivalent — a
``LoopState`` row that pauses/disables the ``dispatch`` always-on loop, the loop
the in-session Stop self-pump exists to drive.

When that row says PAUSED or DISABLED the self-pump must suppress (a clean
no-op) so a paused loop stays paused across a session restart, exactly as the
env kill-switch suppresses it within a process. An empty table / an ENABLED row
leaves the self-pump behaviour unchanged (no regression), and a DB read failure
fails OPEN (the env/availability/ownership gates still decide) so the Stop hook
can never crash on an unreadable database.
"""

import os
import time
from pathlib import Path

import pytest

import hooks.scripts.hook_router as router
import hooks.scripts.loop_state_self_pump_gate as gate
from hooks.scripts.hook_router import _OWNER_LOOP, _write_loop_registry, handle_loop_self_pump

pytestmark = pytest.mark.django_db


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


class TestSelfPumpHonoursDbLoopState:
    def test_db_paused_dispatch_loop_makes_owner_stop_hook_a_noop(self, monkeypatch: pytest.MonkeyPatch) -> None:
        from teatree.core.models import LoopState  # noqa: PLC0415

        LoopState.objects.pause("dispatch")
        _own_loop("owner-1")
        _fake_pending(monkeypatch, [{"task_id": 4, "subagent": "x", "phase": "coding", "issue_url": "u"}])

        result = handle_loop_self_pump({"session_id": "owner-1"})

        assert result is not True  # paused: no block, the session may end

    def test_db_disabled_dispatch_loop_makes_owner_stop_hook_a_noop(self, monkeypatch: pytest.MonkeyPatch) -> None:
        from teatree.core.models import LoopState  # noqa: PLC0415

        LoopState.objects.disable("dispatch")
        _own_loop("owner-1")
        _fake_pending(monkeypatch, [{"task_id": 4, "subagent": "x", "phase": "coding", "issue_url": "u"}])

        result = handle_loop_self_pump({"session_id": "owner-1"})

        assert result is not True

    def test_db_paused_dispatch_loop_does_not_probe_pending_work(self, monkeypatch: pytest.MonkeyPatch) -> None:
        from teatree.core.models import LoopState  # noqa: PLC0415

        LoopState.objects.pause("dispatch")
        _own_loop("owner-1")
        probed = {"called": False}

        def _spy() -> list[dict]:
            probed["called"] = True
            return [{"task_id": 1, "subagent": "x", "phase": "c", "issue_url": "u"}]

        monkeypatch.setattr(router, "_consolidated_pending_work", _spy)

        result = handle_loop_self_pump({"session_id": "owner-1"})

        assert probed["called"] is False  # gate checked BEFORE the subprocess
        assert result is not True

    def test_empty_table_leaves_owner_pumping(self, monkeypatch: pytest.MonkeyPatch) -> None:
        # No LoopState row → no regression: the owner with pending work pumps.
        _own_loop("owner-1")
        _fake_pending(monkeypatch, [{"task_id": 4, "subagent": "x", "phase": "coding", "issue_url": "u"}])

        result = handle_loop_self_pump({"session_id": "owner-1"})

        assert result is True

    def test_db_enabled_dispatch_loop_leaves_owner_pumping(self, monkeypatch: pytest.MonkeyPatch) -> None:
        from teatree.core.models import LoopState  # noqa: PLC0415

        LoopState.objects.pause("dispatch")
        LoopState.objects.resume("dispatch")
        _own_loop("owner-1")
        _fake_pending(monkeypatch, [{"task_id": 4, "subagent": "x", "phase": "coding", "issue_url": "u"}])

        result = handle_loop_self_pump({"session_id": "owner-1"})

        assert result is True

    def test_paused_other_loop_does_not_suppress_the_pump(self, monkeypatch: pytest.MonkeyPatch) -> None:
        # The self-pump drives the always-on ``dispatch`` loop; pausing an
        # UNRELATED named loop (e.g. ``review``) must not silence the pump.
        from teatree.core.models import LoopState  # noqa: PLC0415

        LoopState.objects.pause("review")
        _own_loop("owner-1")
        _fake_pending(monkeypatch, [{"task_id": 4, "subagent": "x", "phase": "coding", "issue_url": "u"}])

        result = handle_loop_self_pump({"session_id": "owner-1"})

        assert result is True

    def test_db_read_failure_fails_open_pump_proceeds(self, monkeypatch: pytest.MonkeyPatch) -> None:
        # A Stop hook must be crash-proof: if the DB read raises, the gate
        # fails OPEN (defers to env/availability/ownership) and the pump runs.
        monkeypatch.setattr(gate, "bootstrap_teatree_django", lambda: False)
        _own_loop("owner-1")
        _fake_pending(monkeypatch, [{"task_id": 4, "subagent": "x", "phase": "coding", "issue_url": "u"}])

        result = handle_loop_self_pump({"session_id": "owner-1"})

        assert result is True
