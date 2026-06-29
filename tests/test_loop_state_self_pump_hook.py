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

#2559 changed the READ MECHANISM: the bare-``python3`` Stop hook cannot
``django.setup()``, so the gate no longer reads the ``LoopState`` row through the
ORM in-process — it shells out to ``t3 loop loop-state dispatch --json`` (the
``t3`` child carries its own venv and bootstraps Django). So these tests stub the
``t3`` subprocess (the external boundary) rather than writing an ORM row, which a
child ``t3`` process would not see in the pytest-django transactional test DB.
"""

import json
import os
import subprocess
import sys
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

import hooks.scripts.hook_router as router
from hooks.scripts.hook_router import _OWNER_LOOP, _write_loop_registry, handle_loop_self_pump

# ``hook_router`` binds the gate function via a bare ``from
# loop_state_self_pump_gate import …`` (its own dir on sys.path), so the live
# instance is ``sys.modules["loop_state_self_pump_gate"]`` — a SEPARATE object
# from ``hooks.scripts.loop_state_self_pump_gate`` (the dual identity called out
# in ``hooks/CLAUDE.md``). Patch the instance the router actually calls.
gate = sys.modules["loop_state_self_pump_gate"]


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


def _fake_loop_state(
    monkeypatch: pytest.MonkeyPatch,
    *,
    dispatch_status: str | None = None,
    crash: bool = False,
) -> dict[str, int]:
    """Stub the gate's ``t3 loop loop-state dispatch --json`` subprocess.

    *dispatch_status* is the durable status the ``t3`` child reports for the
    ``dispatch`` loop (``enabled`` / ``paused`` / ``disabled``); ``None`` (the
    default) means "loop not queried in this case" — a non-``dispatch`` query
    returns ENABLED. *crash* simulates an unreadable control plane (the read
    raises) so the gate fails OPEN. Returns a probe counter so a test can prove
    the gate short-circuits BEFORE the ``pending-spawn`` subprocess.
    """
    counter = {"loop_state_calls": 0}

    monkeypatch.setattr(gate, "shutil", SimpleNamespace(which=lambda _name: "/usr/local/bin/t3"))

    def _run(argv: list[str], *_args: object, **_kwargs: object) -> SimpleNamespace:
        if crash:
            msg = "control plane unreadable"
            raise OSError(msg)
        joined = " ".join(argv)
        if "loop-state" in joined:
            counter["loop_state_calls"] += 1
            # The argv is ``t3 loop loop-state <name> --json``; report the
            # requested loop's status (ENABLED for any loop other than dispatch).
            name = argv[3] if len(argv) > 3 else ""
            status = dispatch_status if (name == "dispatch" and dispatch_status) else "enabled"
            return SimpleNamespace(returncode=0, stdout=json.dumps({"name": name, "status": status}), stderr="")
        return SimpleNamespace(returncode=1, stdout="", stderr="")

    monkeypatch.setattr(gate, "subprocess", SimpleNamespace(run=_run, TimeoutExpired=subprocess.TimeoutExpired))
    return counter


class TestSelfPumpHonoursDbLoopState:
    def test_db_paused_dispatch_loop_makes_owner_stop_hook_a_noop(self, monkeypatch: pytest.MonkeyPatch) -> None:
        _fake_loop_state(monkeypatch, dispatch_status="paused")
        _own_loop("owner-1")
        _fake_pending(monkeypatch, [{"task_id": 4, "subagent": "x", "phase": "coding", "issue_url": "u"}])

        result = handle_loop_self_pump({"session_id": "owner-1"})

        assert result is not True  # paused: no block, the session may end

    def test_db_disabled_dispatch_loop_makes_owner_stop_hook_a_noop(self, monkeypatch: pytest.MonkeyPatch) -> None:
        _fake_loop_state(monkeypatch, dispatch_status="disabled")
        _own_loop("owner-1")
        _fake_pending(monkeypatch, [{"task_id": 4, "subagent": "x", "phase": "coding", "issue_url": "u"}])

        result = handle_loop_self_pump({"session_id": "owner-1"})

        assert result is not True

    def test_db_paused_dispatch_loop_does_not_probe_pending_work(self, monkeypatch: pytest.MonkeyPatch) -> None:
        _fake_loop_state(monkeypatch, dispatch_status="paused")
        _own_loop("owner-1")
        probed = {"called": False}

        def _spy() -> list[dict]:
            probed["called"] = True
            return [{"task_id": 1, "subagent": "x", "phase": "c", "issue_url": "u"}]

        monkeypatch.setattr(router, "_consolidated_pending_work", _spy)

        result = handle_loop_self_pump({"session_id": "owner-1"})

        assert probed["called"] is False  # gate checked BEFORE the pending probe
        assert result is not True

    def test_empty_state_leaves_owner_pumping(self, monkeypatch: pytest.MonkeyPatch) -> None:
        # No durable row → ``t3`` reports ENABLED → no regression: the owner with
        # pending work pumps.
        _fake_loop_state(monkeypatch, dispatch_status="enabled")
        _own_loop("owner-1")
        _fake_pending(monkeypatch, [{"task_id": 4, "subagent": "x", "phase": "coding", "issue_url": "u"}])

        result = handle_loop_self_pump({"session_id": "owner-1"})

        assert result is True

    def test_db_enabled_dispatch_loop_leaves_owner_pumping(self, monkeypatch: pytest.MonkeyPatch) -> None:
        _fake_loop_state(monkeypatch, dispatch_status="enabled")
        _own_loop("owner-1")
        _fake_pending(monkeypatch, [{"task_id": 4, "subagent": "x", "phase": "coding", "issue_url": "u"}])

        result = handle_loop_self_pump({"session_id": "owner-1"})

        assert result is True

    def test_paused_other_loop_does_not_suppress_the_pump(self, monkeypatch: pytest.MonkeyPatch) -> None:
        # The self-pump drives the always-on ``dispatch`` loop; the gate queries
        # ONLY ``dispatch`` — a paused UNRELATED loop is invisible to it, so the
        # pump still fires. ``_fake_loop_state`` reports ENABLED for any non-
        # dispatch loop, so a dispatch query here resolves runnable.
        _fake_loop_state(monkeypatch, dispatch_status="enabled")
        _own_loop("owner-1")
        _fake_pending(monkeypatch, [{"task_id": 4, "subagent": "x", "phase": "coding", "issue_url": "u"}])

        result = handle_loop_self_pump({"session_id": "owner-1"})

        assert result is True

    def test_control_plane_read_failure_fails_open_pump_proceeds(self, monkeypatch: pytest.MonkeyPatch) -> None:
        # A Stop hook must be crash-proof: if the control-plane read raises, the
        # gate fails OPEN (defers to env/availability/ownership) and the pump
        # runs. Mirrors the #2559 stdlib read failing safe.
        _fake_loop_state(monkeypatch, crash=True)
        _own_loop("owner-1")
        _fake_pending(monkeypatch, [{"task_id": 4, "subagent": "x", "phase": "coding", "issue_url": "u"}])

        result = handle_loop_self_pump({"session_id": "owner-1"})

        assert result is True

    def test_t3_absent_fails_open_pump_proceeds(self, monkeypatch: pytest.MonkeyPatch) -> None:
        # No ``t3`` binary on PATH ⇒ the control plane is genuinely unreadable ⇒
        # fail OPEN, the pump runs (the other gates still decide).
        monkeypatch.setattr(gate, "shutil", SimpleNamespace(which=lambda _name: None))
        _own_loop("owner-1")
        _fake_pending(monkeypatch, [{"task_id": 4, "subagent": "x", "phase": "coding", "issue_url": "u"}])

        result = handle_loop_self_pump({"session_id": "owner-1"})

        assert result is True
