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

    def test_reachable_but_unreadable_control_plane_fails_closed_suppresses(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # #2777 L3: when ``t3`` IS on PATH but the read raises, the control plane
        # is reachable-but-unreadable ⇒ INDETERMINATE ⇒ FAIL CLOSED (suppress), so
        # a transient read failure can never nag the loop through a possible pause
        # (pause must win, matching ``_pause_suppresses_self_pump``). RED on main,
        # which failed OPEN here and pumped.
        _fake_loop_state(monkeypatch, crash=True)
        _own_loop("owner-1")
        _fake_pending(monkeypatch, [{"task_id": 4, "subagent": "x", "phase": "coding", "issue_url": "u"}])

        result = handle_loop_self_pump({"session_id": "owner-1"})

        assert result is not True

    def test_t3_absent_fails_open_pump_proceeds(self, monkeypatch: pytest.MonkeyPatch) -> None:
        # The ONE carve-out: no ``t3`` binary on PATH ⇒ the loop genuinely cannot
        # run ``t3`` at all (a loop that can't run t3 can't be paused) ⇒ fail OPEN,
        # the pump runs (the other gates still decide).
        monkeypatch.setattr(gate, "shutil", SimpleNamespace(which=lambda _name: None))
        _own_loop("owner-1")
        _fake_pending(monkeypatch, [{"task_id": 4, "subagent": "x", "phase": "coding", "issue_url": "u"}])

        result = handle_loop_self_pump({"session_id": "owner-1"})

        assert result is True


class TestGateFailDirectionSplit:
    """``db_loop_state_suppresses_self_pump`` splits binary-absent (OPEN) from unreadable (CLOSED).

    #2777 L3: the fix narrows the fail direction. ``_dispatch_loop_status`` now
    returns ``None`` ONLY for a binary-absent control plane (fail OPEN) and ``""``
    for a present-but-unreadable one (fail CLOSED / suppress). The suppress
    predicate maps that split:
    ``None`` → pump (carve-out); ``""`` / ``paused`` / ``disabled`` → suppress;
    ``enabled`` → pump.
    """

    def test_binary_absent_status_is_none(self, monkeypatch: pytest.MonkeyPatch) -> None:
        # RED on main: ``_dispatch_loop_status`` returned ``""`` for binary-absent,
        # conflating it with present-but-unreadable. Now it returns ``None``.
        monkeypatch.setattr(gate, "shutil", SimpleNamespace(which=lambda _name: None))
        assert gate._dispatch_loop_status() is None

    def test_present_but_unreadable_suppresses(self, monkeypatch: pytest.MonkeyPatch) -> None:
        # RED on main: ``""`` resolved to ``False`` (fail OPEN). Now it suppresses.
        monkeypatch.setattr(gate, "_dispatch_loop_status", lambda: "")
        assert gate.db_loop_state_suppresses_self_pump() is True

    def test_binary_absent_does_not_suppress(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(gate, "_dispatch_loop_status", lambda: None)
        assert gate.db_loop_state_suppresses_self_pump() is False

    @pytest.mark.parametrize(
        ("status", "suppressed"),
        [("enabled", False), ("paused", True), ("disabled", True)],
    )
    def test_resolved_status_maps_to_suppress(
        self, status: str, monkeypatch: pytest.MonkeyPatch, *, suppressed: bool
    ) -> None:
        monkeypatch.setattr(gate, "_dispatch_loop_status", lambda: status)
        assert gate.db_loop_state_suppresses_self_pump() is suppressed

    def test_self_pump_suppressed_composes_the_unreadable_close(self, monkeypatch: pytest.MonkeyPatch) -> None:
        # Composition: a reachable-but-unreadable control plane gates the owner's
        # self-pump off via ``_self_pump_suppressed`` (the gate is checked first).
        # RED on main: ``""`` failed open, so the owner kept pumping.
        _own_loop("owner-1")
        monkeypatch.setattr(router, "_pause_suppresses_self_pump", lambda: False)
        monkeypatch.setattr(gate, "_dispatch_loop_status", lambda: "")
        assert router._self_pump_suppressed("owner-1") is True
