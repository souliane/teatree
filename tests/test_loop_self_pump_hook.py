"""Tests for the per-session loop self-pump Stop hook (#758 / board #50).

The self-pump replaces the manual coordinator pump: when the loop-owner
session finishes a turn and consolidated work remains, the Stop hook
emits ``{"decision": "block", "reason": ...}`` to self-continue the loop
without an external re-prompt. No pending work => no block (idle, by
design — mirrors #748 "zero sessions = dead, accepted"). Non-owner
sessions never pump (registry dedup). Anti-spin via a per-session marker
+ mtime min-interval. ``SessionEnd`` clears the marker.

Integration-style: real ``hook_router`` handler, real ``STATE_DIR`` +
``T3_LOOP_REGISTRY_DIR`` redirected to ``tmp_path``; only the
``pending-spawn`` subprocess (an external boundary) is faked.
"""

import json
import os
import time
from pathlib import Path

import pytest

import hooks.scripts.hook_router as router
from hooks.scripts.hook_router import _write_loop_registry, handle_loop_self_pump, handle_session_end_self_pump


@pytest.fixture(autouse=True)
def _isolation(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    state = tmp_path / "state"
    state.mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr(router, "STATE_DIR", state)
    monkeypatch.setenv("T3_LOOP_REGISTRY_DIR", str(tmp_path / "data"))
    (tmp_path / "data").mkdir(parents=True, exist_ok=True)


def _own_loop(session_id: str) -> None:
    _write_loop_registry(
        {
            "t3-main-loop": {
                "session_id": session_id,
                "agent_id": "a",
                "pid": os.getpid(),
                "spawn_brief": "t3-main-loop brief",
                "heartbeat_ts": int(time.time()),
            }
        }
    )


def _fake_pending(monkeypatch: pytest.MonkeyPatch, entries: list[dict]) -> None:
    monkeypatch.setattr(router, "_consolidated_pending_work", lambda: entries)


def _decision(capsys: pytest.CaptureFixture[str]) -> dict:
    out = capsys.readouterr().out.strip()
    return json.loads(out) if out else {}


class TestLoopSelfPump:
    def test_owner_with_pending_work_blocks_to_self_continue(
        self, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _own_loop("owner-1")
        _fake_pending(monkeypatch, [{"task_id": 7, "subagent": "t3:orchestrator", "phase": "coding", "issue_url": "u"}])

        result = handle_loop_self_pump({"session_id": "owner-1"})

        decision = _decision(capsys)
        assert decision.get("decision") == "block"
        assert "loop" in decision.get("reason", "").lower()
        # The consolidated work is carried into the re-pump directive.
        assert "7" in decision["reason"] or "pending" in decision["reason"].lower()
        # Short-circuits the handler chain (a decision was emitted).
        assert result is True

    def test_owner_with_no_pending_work_does_not_block(
        self, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _own_loop("owner-1")
        _fake_pending(monkeypatch, [])

        result = handle_loop_self_pump({"session_id": "owner-1"})

        assert _decision(capsys) == {}
        assert result is not True  # idle: no block, session may end

    def test_non_owner_session_never_pumps(
        self, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _own_loop("the-owner")
        _fake_pending(monkeypatch, [{"task_id": 1, "subagent": "x", "phase": "coding", "issue_url": "u"}])

        result = handle_loop_self_pump({"session_id": "a-different-session"})

        assert _decision(capsys) == {}
        assert result is not True

    def test_anti_spin_suppresses_immediate_repeat(
        self, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _own_loop("owner-1")
        _fake_pending(monkeypatch, [{"task_id": 9, "subagent": "x", "phase": "coding", "issue_url": "u"}])

        first = handle_loop_self_pump({"session_id": "owner-1"})
        capsys.readouterr()
        second = handle_loop_self_pump({"session_id": "owner-1"})

        assert first is True
        # A second Stop within the min-interval must not re-pump (no spin).
        assert _decision(capsys) == {}
        assert second is not True

    def test_anti_spin_releases_after_min_interval(
        self, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _own_loop("owner-1")
        _fake_pending(monkeypatch, [{"task_id": 9, "subagent": "x", "phase": "coding", "issue_url": "u"}])
        handle_loop_self_pump({"session_id": "owner-1"})
        capsys.readouterr()

        marker = router.STATE_DIR / "owner-1.pump-armed"
        old = time.time() - router._SELF_PUMP_MIN_INTERVAL - 5
        os.utime(marker, (old, old))

        result = handle_loop_self_pump({"session_id": "owner-1"})

        assert _decision(capsys).get("decision") == "block"
        assert result is True

    def test_no_session_id_is_noop(self, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch) -> None:
        _fake_pending(monkeypatch, [{"task_id": 1, "subagent": "x", "phase": "c", "issue_url": "u"}])
        result = handle_loop_self_pump({"session_id": ""})
        assert _decision(capsys) == {}
        assert result is not True


class TestSessionEndClearsPumpMarker:
    def test_session_end_removes_marker(self, monkeypatch: pytest.MonkeyPatch) -> None:
        _own_loop("owner-1")
        _fake_pending(monkeypatch, [{"task_id": 3, "subagent": "x", "phase": "c", "issue_url": "u"}])
        handle_loop_self_pump({"session_id": "owner-1"})
        marker = router.STATE_DIR / "owner-1.pump-armed"
        assert marker.is_file()

        handle_session_end_self_pump({"session_id": "owner-1"})

        assert not marker.exists()

    def test_session_end_no_session_id_is_noop(self) -> None:
        handle_session_end_self_pump({"session_id": ""})  # must not raise


class TestWiredIntoRouter:
    def test_stop_event_registered_in_handlers(self) -> None:
        assert "Stop" in router._HANDLERS
        assert handle_loop_self_pump in router._HANDLERS["Stop"]

    def test_session_end_self_pump_registered(self) -> None:
        assert handle_session_end_self_pump in router._HANDLERS["SessionEnd"]

    def test_hooks_json_declares_stop_event(self) -> None:
        hooks_json = Path(router.__file__).resolve().parents[2] / "hooks" / "hooks.json"
        config = json.loads(hooks_json.read_text(encoding="utf-8"))
        assert "Stop" in config["hooks"]


class TestCleanupStalePumpArmed:
    """#758 N1: a crashed session's stale ``*.pump-armed`` is swept.

    Its mere presence would suppress a new owner's self-pump (the
    anti-spin check keys on the marker existing); the current session's
    marker is kept.
    """

    def test_sweeps_other_session_pump_armed_keeps_own(self) -> None:
        (router.STATE_DIR / "dead-sess.pump-armed").write_text("1", encoding="utf-8")
        (router.STATE_DIR / "dead-sess.loop-pending").write_text("1", encoding="utf-8")
        (router.STATE_DIR / "live-sess.pump-armed").write_text("1", encoding="utf-8")

        router._cleanup_stale_pending("live-sess")

        assert not (router.STATE_DIR / "dead-sess.pump-armed").exists()
        assert not (router.STATE_DIR / "dead-sess.loop-pending").exists()
        assert (router.STATE_DIR / "live-sess.pump-armed").exists()
