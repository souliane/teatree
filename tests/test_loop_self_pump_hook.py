"""Tests for the loop self-pump Stop hook (#758 / board #50 / #786 WS4).

The self-pump replaces the manual coordinator pump: when an agent
finishes a turn and consolidated work remains, the Stop hook emits
``{"decision": "block", "reason": ...}`` to self-continue the loop
without an external re-prompt. No pending work => no block (idle, by
design — mirrors #748 "zero sessions = dead, accepted"). Anti-spin via a
marker + mtime min-interval. ``SessionEnd`` clears the marker.

#786 WS4 (invariant 3) changed the dedup axis: the consolidation loop is
exactly one *per agent identity across all sessions* — NOT the single
global tick-owner session. The cross-session/per-agent dedup contract is
covered in ``test_per_agent_consolidation_loop.py``; this module covers
the non-dedup mechanics (block emission + pending summary, anti-spin,
no-work idle, the #810 crash-safe fail-open, router wiring, stale-marker
cleanup).

Integration-style: real ``hook_router`` handler, real ``STATE_DIR`` +
``T3_LOOP_REGISTRY_DIR`` redirected to ``tmp_path``; only the
``pending-spawn`` subprocess (an external boundary) is faked.
"""

import contextlib
import json
import os
import sys
import time
from collections.abc import Iterator
from pathlib import Path

import pytest

import hooks.scripts.hook_router as router
from hooks.scripts.hook_router import (
    _OWNER_LOOP,
    _write_loop_registry,
    handle_loop_self_pump,
    handle_session_end_self_pump,
)


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

    def test_pump_directive_tags_tick_with_owner_session_id(
        self, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The self-pump tick command carries the owner session id (#1073 follow-up).

        The owner's tick must claim under its real session id (and record
        its pid) instead of resolving to ``""`` in a Bash subprocess
        (#1107). Prefixing the emitted ``t3 loop tick`` with
        ``T3_LOOP_SESSION_ID=<session>`` guarantees the re-claim heartbeat
        keeps the lease anchored to the owner.
        """
        _own_loop("owner-1")
        _fake_pending(monkeypatch, [{"task_id": 7, "subagent": "x", "phase": "coding", "issue_url": "u"}])

        handle_loop_self_pump({"session_id": "owner-1"})

        reason = _decision(capsys)["reason"]
        assert "T3_LOOP_SESSION_ID=owner-1 t3 loop tick" in reason

    def test_owner_with_no_pending_work_does_not_block(
        self, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _own_loop("owner-1")
        _fake_pending(monkeypatch, [])

        result = handle_loop_self_pump({"session_id": "owner-1"})

        assert _decision(capsys) == {}
        assert result is not True  # idle: no block, session may end

    # #959: the self-pump is a SINGLETON bound to the one designated
    # loop-owner session (the ``_OWNER_LOOP`` record set at SessionStart).
    # The WS4 "per-agent, decoupled from the tick-owner" decoupling leaked
    # the loop into every fresh/unrelated session: a brand-new blog-writing
    # session immediately started pumping ``t3 loop tick`` / ``claim-next``.
    # A non-owner session's Stop hook MUST be a clean no-op (no pump, no
    # subprocess, no error noise) — the per-agent consolidation slot is a
    # secondary dedup, NOT a substitute for the owner gate.

    def test_non_owner_session_never_pumps(
        self, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # A DIFFERENT live session owns the loop; this fresh, unrelated
        # session has pending work but must NOT pump.
        _own_loop("owner-1")
        _fake_pending(monkeypatch, [{"task_id": 9, "subagent": "x", "phase": "coding", "issue_url": "u"}])

        result = handle_loop_self_pump({"session_id": "blog-session", "agent_id": "blog-agent"})

        assert _decision(capsys) == {}  # clean no-op: no block decision
        assert result is not True

    def test_non_owner_session_does_not_probe_pending_work(
        self, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # The owner gate is checked BEFORE any pending-spawn subprocess —
        # a non-owner session must not even shell out to ``t3``.
        _own_loop("owner-1")
        probed = {"called": False}

        def _spy() -> list[dict]:
            probed["called"] = True
            return [{"task_id": 1, "subagent": "x", "phase": "c", "issue_url": "u"}]

        monkeypatch.setattr(router, "_consolidated_pending_work", _spy)

        result = handle_loop_self_pump({"session_id": "other-session"})

        assert probed["called"] is False
        assert _decision(capsys) == {}
        assert result is not True

    def test_no_owner_recorded_is_a_clean_noop(
        self, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # No ``_OWNER_LOOP`` record at all (registry empty) ⇒ no session is
        # the designated owner ⇒ nobody pumps. (SessionStart designates an
        # owner; absent that, the loop is idle by design.)
        _fake_pending(monkeypatch, [{"task_id": 5, "subagent": "x", "phase": "c", "issue_url": "u"}])

        result = handle_loop_self_pump({"session_id": "any-session"})

        assert _decision(capsys) == {}
        assert result is not True

    def test_disown_env_var_makes_owner_stop_hook_a_noop(
        self, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # Immediate mitigation: even the owner session can release the
        # loop in-process by exporting ``T3_LOOP_DISOWN=1`` — the Stop
        # hook becomes a clean no-op without touching the registry.
        _own_loop("owner-1")
        _fake_pending(monkeypatch, [{"task_id": 2, "subagent": "x", "phase": "c", "issue_url": "u"}])
        monkeypatch.setenv("T3_LOOP_DISOWN", "1")

        result = handle_loop_self_pump({"session_id": "owner-1"})

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


class TestStopHookFailsSafeWithoutTeatree:
    """#810: a ``Stop`` hook must never raise to the session.

    Hooks run under whatever interpreter the agent harness invokes;
    ``teatree`` importability is NOT guaranteed there. The lazy
    ``from teatree.utils.singleton import pid_alive`` in
    ``_prune_dead_owner`` crashed a live session with
    ``ModuleNotFoundError: No module named 'teatree'`` and surfaced a
    full traceback. The Stop path must degrade gracefully (treat loop
    ownership as unknown / skip the self-pump) on a missing or
    unimportable ``teatree``.
    """

    @staticmethod
    @contextlib.contextmanager
    def _teatree_unimportable() -> Iterator[None]:
        """Make ``import teatree*`` raise ``ModuleNotFoundError``.

        Faithfully reproduces the hook-interpreter env where ``teatree``
        is absent from ``sys.path``: purge any cached ``teatree`` modules
        and install a ``meta_path`` finder that refuses to resolve them.
        """

        class _BlockTeatree:
            def find_spec(self, name: str, path: object = None, target: object = None) -> None:
                if name == "teatree" or name.startswith("teatree."):
                    msg = f"No module named {name!r}"
                    raise ModuleNotFoundError(msg)

        saved = {k: v for k, v in sys.modules.items() if k == "teatree" or k.startswith("teatree.")}
        for k in saved:
            del sys.modules[k]
        finder = _BlockTeatree()
        sys.meta_path.insert(0, finder)
        try:
            yield
        finally:
            with contextlib.suppress(ValueError):
                sys.meta_path.remove(finder)
            sys.modules.update(saved)

    def test_self_pump_skips_when_teatree_unimportable(
        self, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _own_loop("owner-x")
        _fake_pending(monkeypatch, [{"task_id": 1, "subagent": "x", "phase": "coding", "issue_url": "u"}])

        with self._teatree_unimportable():
            # Pre-guard this raises ModuleNotFoundError straight to the
            # caller (the Stop dispatch loop) — a session-disrupting
            # traceback. Post-guard it must return cleanly.
            result = handle_loop_self_pump({"session_id": "owner-x"})

        assert result is None
        # Self-pump skipped: no block decision emitted.
        assert _decision(capsys) == {}

    def test_session_owns_loop_false_when_teatree_unimportable(self) -> None:
        _own_loop("owner-y")
        with self._teatree_unimportable():
            assert router._session_owns_loop("owner-y") is False

    def test_prune_dead_owner_degrades_when_teatree_unimportable(self) -> None:
        registry = {_OWNER_LOOP: {"session_id": "s", "pid": os.getpid()}}
        with self._teatree_unimportable():
            # Ownership unknown => empty registry (no entry can be
            # confirmed live without the pid-liveness primitive).
            assert router._prune_dead_owner(registry) == {}

    def test_boundary_guard_contains_any_unexpected_stop_path_error(
        self, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Belt-and-suspenders boundary guard.

        ANY unexpected error in the Stop path (not just a missing
        ``teatree``) is contained — the broad boundary guard returns
        ``None`` instead of raising to the session.
        """

        def _boom(_data: dict) -> bool | None:
            msg = "unexpected Stop-path failure"
            raise RuntimeError(msg)

        monkeypatch.setattr(router, "_loop_self_pump", _boom)

        result = handle_loop_self_pump({"session_id": "owner-z"})

        assert result is None
        assert _decision(capsys) == {}


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
