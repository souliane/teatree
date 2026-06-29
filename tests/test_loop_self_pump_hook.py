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
from types import SimpleNamespace

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
    # Point the bash-env-file fallback at a path that does not exist so a
    # developer's real ``~/.teatree`` (which may set ``T3_LOOP_DISOWN``) never
    # leaks into a test that does not opt in. Cases that exercise the file
    # override it explicitly.
    monkeypatch.setenv("TEATREE_BASH_ENV_FILE", str(tmp_path / "no-bash-env"))
    # The DB LoopState gate is owned by test_loop_state_self_pump_hook.py (which
    # fakes the ``t3`` subprocess). These tests exercise the OTHER suppression
    # paths (ownership, disown, away, anti-spin), so isolate them from the live
    # control plane — otherwise a real ``t3`` whose ``dispatch`` loop is paused
    # (the #2777 L3 fail-closed) suppresses the pump these tests assert fires.
    monkeypatch.setattr(router, "db_loop_state_suppresses_self_pump", lambda: False)


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
        """The self-pump tick command carries the owner session id and pid (#1073/#1722).

        The owner's tick must claim under its real session id AND its
        durable session pid instead of resolving the id to ``""`` and the
        pid to ``os.getppid()`` of the torn-down Bash subprocess (#1107/
        #1722). Exporting both ``T3_LOOP_SESSION_ID`` and
        ``T3_LOOP_SESSION_PID`` keeps the re-claim heartbeat anchored to the
        owner even when the tick subprocess cannot read the loop registry.
        """
        _own_loop("owner-1")
        _fake_pending(monkeypatch, [{"task_id": 7, "subagent": "x", "phase": "coding", "issue_url": "u"}])

        monkeypatch.setattr(os, "getppid", lambda: 4242)
        handle_loop_self_pump({"session_id": "owner-1"})

        reason = _decision(capsys)["reason"]
        # #2777 cutover: the self-pump fires the bare master `t3 loops tick`
        # (claims loop-owner + loop-tick — behaviour-preserving vs the retired
        # `t3 loop tick`), not the legacy fat-tick spelling.
        assert "T3_LOOP_SESSION_ID=owner-1 T3_LOOP_SESSION_PID=4242 t3 loops tick" in reason

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

    def test_env_kill_switch_all_is_inert(
        self, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # ``T3_LOOPS_DISABLED`` is removed — a set env var has NO effect on the
        # self-pump: the owner with pending work STILL pumps. A durable DB
        # ``LoopState`` DISABLE of ``dispatch`` is the control outcome (pinned by
        # test_loop_state_self_pump_hook.py).
        _own_loop("owner-1")
        _fake_pending(monkeypatch, [{"task_id": 4, "subagent": "x", "phase": "coding", "issue_url": "u"}])
        monkeypatch.setenv("T3_LOOPS_DISABLED", "all")

        result = handle_loop_self_pump({"session_id": "owner-1"})

        assert _decision(capsys).get("decision") == "block"
        assert result is True

    def test_disown_from_bash_env_file_makes_owner_a_noop(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # ``T3_LOOP_DISOWN`` set only in the bash env file is likewise honoured
        # by the bare-``python3`` Stop hook.
        env_file = tmp_path / ".teatree"
        env_file.write_text('export T3_LOOP_DISOWN="1"\n', encoding="utf-8")
        monkeypatch.setenv("TEATREE_BASH_ENV_FILE", str(env_file))
        monkeypatch.delenv("T3_LOOP_DISOWN", raising=False)
        monkeypatch.delenv("T3_LOOPS_DISABLED", raising=False)
        _own_loop("owner-1")
        _fake_pending(monkeypatch, [{"task_id": 2, "subagent": "x", "phase": "c", "issue_url": "u"}])

        result = handle_loop_self_pump({"session_id": "owner-1"})

        assert _decision(capsys) == {}
        assert result is not True

    def test_missing_bash_env_file_is_a_clean_pump(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # No env var, no file => not disabled; the owner pumps as before. The
        # crash-proof parse must degrade to "not disabled" on a missing file.
        monkeypatch.setenv("TEATREE_BASH_ENV_FILE", str(tmp_path / "nonexistent"))
        monkeypatch.delenv("T3_LOOPS_DISABLED", raising=False)
        monkeypatch.delenv("T3_LOOP_DISOWN", raising=False)
        _own_loop("owner-1")
        _fake_pending(monkeypatch, [{"task_id": 9, "subagent": "x", "phase": "coding", "issue_url": "u"}])

        result = handle_loop_self_pump({"session_id": "owner-1"})

        assert _decision(capsys).get("decision") == "block"
        assert result is True

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


class TestBashEnvFileResolver:
    """Pure-stdlib parse of the ``export VAR=value`` bash env file (#810).

    The Stop hook process never sources ``~/.teatree``, so the ``T3_LOOP_DISOWN``
    knob set there must be recovered by parsing the file directly — crash-proof,
    with the process env taking precedence. ``T3_LOOP_DISOWN`` is the live
    consumer of ``_resolve_loop_env`` (loop pause/disable lives in the DB
    ``LoopState`` tier now — there is no env kill-switch). The values below are
    arbitrary example strings for the generic resolver.
    """

    def test_env_var_present_short_circuits_file_read(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        env_file = tmp_path / ".teatree"
        env_file.write_text("export T3_LOOP_DISOWN=1\n", encoding="utf-8")
        monkeypatch.setenv("TEATREE_BASH_ENV_FILE", str(env_file))
        monkeypatch.setenv("T3_LOOP_DISOWN", "0")

        assert router._resolve_loop_env("T3_LOOP_DISOWN") == "0"

    def test_falls_back_to_file_when_env_absent(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        env_file = tmp_path / ".teatree"
        env_file.write_text("export T3_LOOP_DISOWN=1\n", encoding="utf-8")
        monkeypatch.setenv("TEATREE_BASH_ENV_FILE", str(env_file))
        monkeypatch.delenv("T3_LOOP_DISOWN", raising=False)

        assert router._resolve_loop_env("T3_LOOP_DISOWN") == "1"

    def test_strips_quotes_inline_comment_and_export(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        env_file = tmp_path / ".teatree"
        env_file.write_text("  export T3_LOOP_DISOWN = '1'  # disown\n", encoding="utf-8")
        monkeypatch.setenv("TEATREE_BASH_ENV_FILE", str(env_file))
        monkeypatch.delenv("T3_LOOP_DISOWN", raising=False)

        assert router._resolve_loop_env("T3_LOOP_DISOWN") == "1"

    def test_last_assignment_wins(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        env_file = tmp_path / ".teatree"
        env_file.write_text("export T3_LOOP_DISOWN=0\nexport T3_LOOP_DISOWN=1\n", encoding="utf-8")
        monkeypatch.setenv("TEATREE_BASH_ENV_FILE", str(env_file))
        monkeypatch.delenv("T3_LOOP_DISOWN", raising=False)

        assert router._resolve_loop_env("T3_LOOP_DISOWN") == "1"

    def test_missing_file_returns_empty(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("TEATREE_BASH_ENV_FILE", str(tmp_path / "nope"))
        monkeypatch.delenv("T3_LOOP_DISOWN", raising=False)

        assert router._resolve_loop_env("T3_LOOP_DISOWN") == ""

    def test_unreadable_file_degrades_to_empty(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        env_file = tmp_path / ".teatree"
        env_file.write_text("export T3_LOOP_DISOWN=1\n", encoding="utf-8")
        monkeypatch.setenv("TEATREE_BASH_ENV_FILE", str(env_file))
        monkeypatch.delenv("T3_LOOP_DISOWN", raising=False)

        def _boom(*_args: object, **_kwargs: object) -> str:
            msg = "unreadable"
            raise OSError(msg)

        monkeypatch.setattr(Path, "read_text", _boom)
        assert router._resolve_loop_env("T3_LOOP_DISOWN") == ""

    def test_double_quoted_value(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        env_file = tmp_path / ".teatree"
        env_file.write_text('export T3_LOOP_DISOWN="1"\n', encoding="utf-8")
        monkeypatch.setenv("TEATREE_BASH_ENV_FILE", str(env_file))
        monkeypatch.delenv("T3_LOOP_DISOWN", raising=False)

        assert router._resolve_loop_env("T3_LOOP_DISOWN") == "1"

    def test_assignment_without_export_keyword(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        env_file = tmp_path / ".teatree"
        env_file.write_text("T3_LOOP_DISOWN=1\n", encoding="utf-8")
        monkeypatch.setenv("TEATREE_BASH_ENV_FILE", str(env_file))
        monkeypatch.delenv("T3_LOOP_DISOWN", raising=False)

        assert router._resolve_loop_env("T3_LOOP_DISOWN") == "1"

    def test_unterminated_quote_keeps_remainder(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        env_file = tmp_path / ".teatree"
        env_file.write_text("export T3_LOOP_DISOWN='1\n", encoding="utf-8")
        monkeypatch.setenv("TEATREE_BASH_ENV_FILE", str(env_file))
        monkeypatch.delenv("T3_LOOP_DISOWN", raising=False)

        assert router._resolve_loop_env("T3_LOOP_DISOWN") == "1"


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


class TestConsolidatedPendingWorkIsClaimAware:
    """TODO #100: the self-pump's work probe must be claim/budget-aware.

    The self-pump re-offered the SAME unit every interval because its
    probe (``t3 loop pending-spawn``) reported EVERY dispatchable PENDING
    task regardless of the admit budget, while ``claim-next`` refuses once
    the in-flight WIP hits the ceiling. So a unit held back by a full
    budget showed as "pending work" forever and the pump re-offered it,
    never advancing. The probe must invoke ``--claimable-only`` so it
    answers "is there a unit a claim could actually take?".
    """

    def _run_with_fake_t3(
        self, monkeypatch: pytest.MonkeyPatch, *, stdout: str, returncode: int = 0
    ) -> tuple[list[dict], list[str]]:
        captured: list[str] = []

        def _fake_run(cmd: list[str], **_kwargs: object) -> SimpleNamespace:
            captured.extend(cmd)
            return SimpleNamespace(returncode=returncode, stdout=stdout)

        monkeypatch.setattr(router.shutil, "which", lambda _name: "/usr/bin/t3")
        monkeypatch.setattr(router.subprocess, "run", _fake_run)
        result = router._consolidated_pending_work()
        return result, captured

    def test_probe_passes_claimable_only_flag(self, monkeypatch: pytest.MonkeyPatch) -> None:
        # The must-not-re-offer contract: the probe is invoked with
        # --claimable-only so a budget-blocked unit never re-arms the pump.
        _result, cmd = self._run_with_fake_t3(
            monkeypatch,
            stdout=json.dumps([{"task_id": 1, "subagent": "x", "phase": "coding", "issue_url": "u"}]),
        )
        assert cmd[:2] == ["/usr/bin/t3", "loop"]
        assert "pending-spawn" in cmd
        assert "--claimable-only" in cmd

    def test_empty_claimable_result_makes_owner_not_pump(
        self, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # ADVANCE-OR-STOP: when the budget-aware probe reports nothing
        # claimable (budget exhausted), the owner does NOT re-offer — the
        # session stops instead of looping on the un-advanceable unit.
        _own_loop("owner-1")
        self._run_with_fake_t3(monkeypatch, stdout="[]")  # primes which/run fakes
        result = handle_loop_self_pump({"session_id": "owner-1"})
        assert _decision(capsys) == {}
        assert result is not True

    def test_claimable_work_still_pumps(
        self, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # Control: a genuinely claimable unit still pumps — the fix narrowed
        # the probe, it did not disable the pump.
        _own_loop("owner-1")
        stdout = json.dumps([{"task_id": 5, "subagent": "x", "phase": "coding", "issue_url": "u"}])
        monkeypatch.setattr(router.shutil, "which", lambda _name: "/usr/bin/t3")
        monkeypatch.setattr(router.subprocess, "run", lambda *_a, **_k: SimpleNamespace(returncode=0, stdout=stdout))

        result = handle_loop_self_pump({"session_id": "owner-1"})
        assert _decision(capsys).get("decision") == "block"
        assert result is True


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


class TestSelfPumpHonorsPause:
    """An explicit user pause wins over the standing loop directive (#2247/#2250).

    The self-pump is teatree's own re-firing Stop directive: it re-emits
    ``{"decision": "block", ...}`` to resume the loop every turn while
    consolidated work remains. When the user has explicitly paused
    (availability resolves to ``away``), that nag must SUPPRESS — the same
    away/present precedence the AskUserQuestion deferral already honours.
    Failing safe means an indeterminate pause signal also suppresses the
    pump (allow the stop) rather than nagging through a pause.
    """

    def _set_pause(self, monkeypatch: pytest.MonkeyPatch, *, suppressed: bool) -> None:
        monkeypatch.setattr(router, "_pause_suppresses_self_pump", lambda: suppressed)

    def test_away_suppresses_the_pump_even_with_pending_work(
        self, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _own_loop("owner-1")
        _fake_pending(monkeypatch, [{"task_id": 7, "subagent": "x", "phase": "coding", "issue_url": "u"}])
        self._set_pause(monkeypatch, suppressed=True)

        result = handle_loop_self_pump({"session_id": "owner-1"})

        assert _decision(capsys) == {}  # no block: the pause wins over the goal
        assert result is not True

    def test_present_still_pumps_as_before(
        self, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # Control: with the pause NOT active, the standing directive fires
        # exactly as before — proves the fix did not just disable the gate.
        _own_loop("owner-1")
        _fake_pending(monkeypatch, [{"task_id": 7, "subagent": "x", "phase": "coding", "issue_url": "u"}])
        self._set_pause(monkeypatch, suppressed=False)

        result = handle_loop_self_pump({"session_id": "owner-1"})

        assert _decision(capsys).get("decision") == "block"
        assert result is True

    def test_away_pump_does_not_probe_pending_work(
        self, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # The pause check short-circuits before any ``pending-spawn``
        # subprocess — a paused owner does not even shell out to ``t3``.
        _own_loop("owner-1")
        probed = {"called": False}

        def _spy() -> list[dict]:
            probed["called"] = True
            return [{"task_id": 1, "subagent": "x", "phase": "c", "issue_url": "u"}]

        monkeypatch.setattr(router, "_consolidated_pending_work", _spy)
        self._set_pause(monkeypatch, suppressed=True)

        result = handle_loop_self_pump({"session_id": "owner-1"})

        assert probed["called"] is False
        assert _decision(capsys) == {}
        assert result is not True

    def test_pause_detection_raising_fails_safe_to_suppress(
        self, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # Fail-safe: if the pause query itself raises, the predicate must
        # resolve to SUPPRESS (indeterminate ⇒ allow stop, never nag through
        # a possible pause). The outer crash-guard would also contain a raise,
        # but the predicate owns the suppress-on-indeterminate direction.
        _own_loop("owner-1")
        _fake_pending(monkeypatch, [{"task_id": 7, "subagent": "x", "phase": "coding", "issue_url": "u"}])

        def _boom() -> bool:
            msg = "availability backend unreachable"
            raise RuntimeError(msg)

        monkeypatch.setattr(router, "_pause_suppresses_self_pump", _boom)

        result = handle_loop_self_pump({"session_id": "owner-1"})

        assert _decision(capsys) == {}  # suppressed, stop allowed
        assert result is None


class TestPauseSuppressionPredicate:
    """``_pause_suppresses_self_pump`` maps availability → suppress decision.

    True (suppress) when the user is away OR the signal is indeterminate;
    False (pump) only when availability resolves cleanly to ``present``.
    """

    def test_away_resolves_to_suppress(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(router, "_resolved_away_mode", lambda: True)
        assert router._pause_suppresses_self_pump() is True

    def test_present_resolves_to_pump(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(router, "_resolved_away_mode", lambda: False)
        assert router._pause_suppresses_self_pump() is False

    def test_availability_read_raising_resolves_to_suppress(self, monkeypatch: pytest.MonkeyPatch) -> None:
        # Guards the predicate's OWN internal try/except: a raising
        # availability read (not a return) must resolve to suppress —
        # indeterminate ⇒ allow stop, never nag through a possible pause.
        # Patching the inner `_resolved_away_mode` (not the whole predicate)
        # is what exercises the internal except rather than the outer
        # handle_loop_self_pump crash-guard.
        def _boom() -> bool:
            msg = "availability backend unreachable"
            raise RuntimeError(msg)

        monkeypatch.setattr(router, "_resolved_away_mode", _boom)
        assert router._pause_suppresses_self_pump() is True
