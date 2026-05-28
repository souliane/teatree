"""Tests for the SessionStart hook handler (tick-dispatch bootstrap).

#718 established a SessionStart hook emitting ``additionalContext``;
#786 WS3 RETIRED the immortal-singleton roster it used to spawn. The
hook now records which single *session* is the loop-tick owner
(Django-free, so the #758/#810 Stop self-pump can gate on it) and emits
a tick-dispatch directive: the loop is the ``t3 loop tick`` cron + WS1
atomic ``claim-next`` + WS2 ``LoopLease``, never a fixed set of
long-lived sub-agents. The ``/rename`` reminder + OSC title stay
owner-only / interactive-TTY-gated.
"""

import json
import os
from pathlib import Path

import pytest

import hooks.scripts.hook_router as router
from hooks.scripts.hook_router import (
    _OWNER_LOOP,
    _loop_registry_path,
    _prune_dead_owner,
    _read_loop_registry,
    _write_loop_registry,
    handle_session_end_loop_registry,
    handle_session_start_bootstrap,
)


@pytest.fixture(autouse=True)
def _isolation(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Point the loop registry + tty sink at temp paths so tests never touch real state.

    Autouse so every test is isolated; returns nothing (the few tests that
    need the concrete paths request the ``registry_paths`` fixture).
    """
    reg_dir = tmp_path / "data"
    reg_dir.mkdir(parents=True, exist_ok=True)
    monkeypatch.setenv("T3_LOOP_REGISTRY_DIR", str(reg_dir))
    # Default: no controlling tty (OSC must NOT fire). Tests opt in explicitly.
    monkeypatch.setattr(router, "_TTY_PATH", str(tmp_path / "fake-tty"))


@pytest.fixture
def registry_paths(tmp_path: Path) -> tuple[Path, Path]:
    """The (registry dir, tty sink) pair the ``_isolation`` fixture configured."""
    return tmp_path / "data", tmp_path / "fake-tty"


def _live_pid() -> int:
    """A pid that is alive for the duration of the test (the test process)."""
    return os.getpid()


def _owner_pid() -> int:
    """The pid the handler records as owner — the hook's parent (the session)."""
    return os.getppid()


class TestLoopRegistry:
    def test_registry_path_under_configured_dir(self, registry_paths) -> None:
        reg_dir, _ = registry_paths
        assert _loop_registry_path() == reg_dir / "loop-registry.json"

    def test_read_missing_registry_returns_empty(self) -> None:
        assert _read_loop_registry() == {}

    def test_write_then_read_roundtrip(self) -> None:
        entry = {"session_id": "s1", "agent_id": "a1", "pid": _live_pid()}
        _write_loop_registry({_OWNER_LOOP: entry})
        assert _read_loop_registry() == {_OWNER_LOOP: entry}

    def test_read_corrupt_registry_returns_empty(self) -> None:
        _loop_registry_path().write_text("{ not json", encoding="utf-8")
        assert _read_loop_registry() == {}

    def test_prune_removes_dead_owner(self) -> None:
        # PID 999999 is (almost certainly) not alive.
        reg = {_OWNER_LOOP: {"session_id": "s1", "agent_id": "a1", "pid": 999999}}
        _write_loop_registry(reg)
        assert _prune_dead_owner(_read_loop_registry()) == {}

    def test_prune_keeps_live_owner(self) -> None:
        reg = {_OWNER_LOOP: {"session_id": "s1", "agent_id": "a1", "pid": _live_pid()}}
        _write_loop_registry(reg)
        assert _prune_dead_owner(_read_loop_registry()) == reg

    def test_owner_loop_is_a_single_key_not_a_roster(self) -> None:
        # #786 WS3: the 4-name immortal roster is retired — _OWNER_LOOP is
        # a single tick-owner-session registry key.
        assert isinstance(_OWNER_LOOP, str)
        assert _OWNER_LOOP == "t3-loop-tick-owner"


class TestHandleSessionStartBootstrap:
    def test_no_session_id_produces_no_output(self, capsys: pytest.CaptureFixture[str]) -> None:
        handle_session_start_bootstrap({})
        assert capsys.readouterr().out == ""

    def test_fresh_machine_is_tick_owner_no_roster_spawn(self, capsys: pytest.CaptureFixture[str]) -> None:
        handle_session_start_bootstrap({"session_id": "owner-1"})

        ctx = json.loads(capsys.readouterr().out)["hookSpecificOutput"]["additionalContext"]
        # Tick-dispatch, NOT the retired roster. Per-unit "spawn one fresh
        # bounded sub-agent" IS the model; what must be gone is the
        # immortal-roster vocabulary + names.
        for name in ("t3-main-loop", "t3-review-loop", "t3-cross-review-loop", "t3-bug-hunt"):
            assert name not in ctx
        for retired in ("re-attach", "reattach", "takeover", "resume by", "from its brief"):
            assert retired not in ctx.lower()
        assert "t3 loop tick" in ctx
        assert "t3 loop claim-next" in ctx
        # Owner gets the rename reminder.
        assert "/rename TEATREE LOOP" in ctx

        owner = _read_loop_registry()[_OWNER_LOOP]
        assert owner["session_id"] == "owner-1"
        # Recorded pid is the SESSION process (hook's parent), not the
        # ephemeral hook subprocess (regression: TestOwnerPidIsSession...).
        assert owner["pid"] == _owner_pid()

    def test_owner_records_agent_id_when_present(self, capsys: pytest.CaptureFixture[str]) -> None:
        handle_session_start_bootstrap({"session_id": "owner-1", "agent_id": "agent-xyz"})
        capsys.readouterr()
        assert _read_loop_registry()[_OWNER_LOOP]["agent_id"] == "agent-xyz"

    def test_second_live_session_stays_idle_no_spawn(self, capsys: pytest.CaptureFixture[str]) -> None:
        _write_loop_registry({_OWNER_LOOP: {"session_id": "owner-1", "agent_id": "agent-owner", "pid": _live_pid()}})

        handle_session_start_bootstrap({"session_id": "second-2"})

        ctx = json.loads(capsys.readouterr().out)["hookSpecificOutput"]["additionalContext"]
        # Non-owner: stay idle, never arm a competing tick, never spawn.
        for retired_token in (
            "t3-main-loop",
            "t3-review-loop",
            "t3-cross-review-loop",
            "t3-bug-hunt",
            "re-attach",
            "reattach",
            "takeover",
            "resume by",
            "from its brief",
            "spawn each",
        ):
            assert retired_token not in ctx.lower()
        assert "another" in ctx.lower()
        assert "owner" in ctx.lower()
        assert "owner-1" in ctx  # names the live owner session
        assert "do not arm" in ctx.lower() or "stay idle" in ctx.lower()
        # #1073 doc-alignment: the directive must state the gate is now
        # HARD (a non-owner tick SKIPs, not "runs and finds nothing").
        assert "skip" in ctx.lower()
        assert "find nothing to claim" not in ctx.lower()
        assert "take" in ctx.lower()
        assert "over" in ctx.lower()
        # Non-owner must NOT get the rename reminder.
        assert "/rename TEATREE LOOP" not in ctx
        # Ownership is unchanged.
        assert _read_loop_registry()[_OWNER_LOOP]["session_id"] == "owner-1"

    def test_same_session_restart_is_idempotent_still_owner(self, capsys: pytest.CaptureFixture[str]) -> None:
        _write_loop_registry({_OWNER_LOOP: {"session_id": "owner-1", "agent_id": "agent-owner", "pid": _live_pid()}})

        handle_session_start_bootstrap({"session_id": "owner-1"})

        ctx = json.loads(capsys.readouterr().out)["hookSpecificOutput"]["additionalContext"]
        # Post-compaction same-session restart: still owner, tick-driven,
        # nothing to re-spawn.
        assert "t3 loop tick" in ctx
        for retired_token in (
            "t3-main-loop",
            "t3-review-loop",
            "t3-cross-review-loop",
            "t3-bug-hunt",
            "re-attach",
            "reattach",
            "takeover",
            "resume by",
            "from its brief",
            "spawn each",
        ):
            assert retired_token not in ctx.lower()
        assert "/rename TEATREE LOOP" in ctx
        assert _read_loop_registry()[_OWNER_LOOP]["session_id"] == "owner-1"

    def test_dead_owner_is_reclaimed_by_new_session(self, capsys: pytest.CaptureFixture[str]) -> None:
        _write_loop_registry({_OWNER_LOOP: {"session_id": "dead-owner", "agent_id": "ghost", "pid": 999999}})

        handle_session_start_bootstrap({"session_id": "new-owner"})

        ctx = json.loads(capsys.readouterr().out)["hookSpecificOutput"]["additionalContext"]
        # Dead owner pruned -> this session becomes tick-owner (no
        # re-spawn; the cron keeps ticking).
        assert "t3 loop tick" in ctx
        for retired_token in (
            "t3-main-loop",
            "t3-review-loop",
            "t3-cross-review-loop",
            "t3-bug-hunt",
            "re-attach",
            "reattach",
            "takeover",
            "resume by",
            "from its brief",
            "spawn each",
        ):
            assert retired_token not in ctx.lower()
        assert _read_loop_registry()[_OWNER_LOOP]["session_id"] == "new-owner"

    def test_owner_with_tty_emits_osc_title(self, registry_paths) -> None:
        _, tty_path = registry_paths
        Path(tty_path).write_text("", encoding="utf-8")

        handle_session_start_bootstrap({"session_id": "owner-1"})

        assert "\033]0;TEATREE LOOP\007" in Path(tty_path).read_text(encoding="utf-8")

    def test_owner_without_tty_does_not_crash_and_skips_osc(
        self, registry_paths, capsys: pytest.CaptureFixture[str]
    ) -> None:
        handle_session_start_bootstrap({"session_id": "owner-1"})
        assert "additionalContext" in json.loads(capsys.readouterr().out)["hookSpecificOutput"]

    def test_non_owner_with_tty_does_not_emit_osc(self, registry_paths) -> None:
        _, tty_path = registry_paths
        Path(tty_path).write_text("", encoding="utf-8")
        _write_loop_registry({_OWNER_LOOP: {"session_id": "owner-1", "agent_id": "agent-owner", "pid": _live_pid()}})

        handle_session_start_bootstrap({"session_id": "non-owner"})

        assert Path(tty_path).read_text(encoding="utf-8") == ""


class TestOwnerPidIsSessionNotHookSubprocess:
    """Regression: the hook router is an ephemeral subprocess.

    Recording ``os.getpid()`` would store a pid dead before any second
    session starts, so ``_prune_dead_owner`` would always evict the owner
    and every session would re-claim — defeating the single-owner
    invariant. The recorded pid must be the long-lived session process
    (the hook's parent).
    """

    def test_recorded_pid_is_parent_not_self(self, capsys: pytest.CaptureFixture[str]) -> None:
        handle_session_start_bootstrap({"session_id": "owner-1"})
        capsys.readouterr()
        assert _read_loop_registry()[_OWNER_LOOP]["pid"] == os.getppid()

    def test_owner_survives_a_simulated_second_session(self, capsys: pytest.CaptureFixture[str]) -> None:
        handle_session_start_bootstrap({"session_id": "owner-1", "agent_id": "agent-1"})
        capsys.readouterr()

        # Session 2 starts while session 1 is still alive -> stay idle,
        # ownership unchanged.
        handle_session_start_bootstrap({"session_id": "owner-2"})
        ctx = json.loads(capsys.readouterr().out)["hookSpecificOutput"]["additionalContext"]
        for retired_token in (
            "t3-main-loop",
            "t3-review-loop",
            "t3-cross-review-loop",
            "t3-bug-hunt",
            "re-attach",
            "reattach",
            "takeover",
            "resume by",
            "from its brief",
            "spawn each",
        ):
            assert retired_token not in ctx.lower()
        assert "owner-1" in ctx
        assert _read_loop_registry()[_OWNER_LOOP]["session_id"] == "owner-1"


class TestSessionEndReleasesOwnership:
    def test_owner_session_end_clears_slot(self) -> None:
        _write_loop_registry({_OWNER_LOOP: {"session_id": "owner-1", "agent_id": "a", "pid": _live_pid()}})
        handle_session_end_loop_registry({"session_id": "owner-1"})
        assert _read_loop_registry() == {}

    def test_non_owner_session_end_keeps_slot(self) -> None:
        reg = {_OWNER_LOOP: {"session_id": "owner-1", "agent_id": "a", "pid": _live_pid()}}
        _write_loop_registry(reg)
        handle_session_end_loop_registry({"session_id": "some-other-session"})
        assert _read_loop_registry() == reg

    def test_session_end_no_session_id_is_noop(self) -> None:
        reg = {_OWNER_LOOP: {"session_id": "owner-1", "agent_id": "a", "pid": _live_pid()}}
        _write_loop_registry(reg)
        handle_session_end_loop_registry({})
        assert _read_loop_registry() == reg

    def test_session_end_empty_registry_is_noop(self) -> None:
        handle_session_end_loop_registry({"session_id": "owner-1"})
        assert _read_loop_registry() == {}


class TestSessionStartWiredIntoRouter:
    def test_session_start_in_handlers_table(self) -> None:
        assert "SessionStart" in router._HANDLERS
        assert handle_session_start_bootstrap in router._HANDLERS["SessionStart"]

    def test_session_end_loop_registry_in_handlers_table(self) -> None:
        assert handle_session_end_loop_registry in router._HANDLERS["SessionEnd"]


class TestWs3TickDispatchContract:
    """#786 WS3: SessionStart retires the immortal-singleton roster.

    The loop is now driven by the ``t3 loop tick`` cron + WS1
    ``claim-next`` (DB-claimed work) + WS2 ``LoopLease`` (one tick-owner),
    NOT by SessionStart spawning/re-spawning a fixed roster of long-lived
    sub-agents. The bootstrap directive must therefore NOT instruct
    spawning the now-retired four-name loop roster, NOT use the
    spawn/takeover/resume/re-attach roster vocabulary, point the session
    at tick-dispatch (the cron drives per-unit fresh bounded sub-agents;
    statelessness across ticks is the compaction-proofing), and still
    emit something (a session needs to know the loop is tick-driven and
    whether it is the tick-owner).
    """

    def _ctx(self, capsys: pytest.CaptureFixture[str], session_id: str = "s-1") -> str:
        handle_session_start_bootstrap({"session_id": session_id})
        return json.loads(capsys.readouterr().out)["hookSpecificOutput"]["additionalContext"]

    def test_bootstrap_does_not_instruct_spawning_the_roster(self, capsys: pytest.CaptureFixture[str]) -> None:
        ctx = self._ctx(capsys)
        # The retired immortal-singleton roster must not be spawned.
        for name in ("t3-main-loop", "t3-review-loop", "t3-cross-review-loop", "t3-bug-hunt"):
            assert name not in ctx, f"retired roster name {name!r} still in bootstrap directive"
        for retired_token in (
            "t3-main-loop",
            "t3-review-loop",
            "t3-cross-review-loop",
            "t3-bug-hunt",
            "re-attach",
            "reattach",
            "takeover",
            "resume by",
            "from its brief",
            "spawn each",
        ):
            assert retired_token not in ctx.lower()

    def test_bootstrap_points_at_tick_dispatch(self, capsys: pytest.CaptureFixture[str]) -> None:
        ctx = self._ctx(capsys).lower()
        # The directive must orient the session toward the tick-driven model.
        assert "tick" in ctx
        assert "t3 loop tick" in ctx or "loop tick" in ctx

    def test_bootstrap_still_emits_a_directive(self, capsys: pytest.CaptureFixture[str]) -> None:
        # A session must still be told the loop is tick-driven; empty
        # output would regress observability.
        assert self._ctx(capsys).strip() != ""

    def test_no_session_id_still_silent(self, capsys: pytest.CaptureFixture[str]) -> None:
        handle_session_start_bootstrap({})
        assert capsys.readouterr().out == ""


# ── Issue #980: auto-compact kill-switch advisory ─────────────────────


class TestAutocompactAdvisoryIntegration:
    """Pin that the SessionStart handler surfaces the #980 advisory.

    The advisory is the teatree-side workaround for the harness's
    silent auto-compact kill-switch on 1M-capable models (see
    ``teatree.core.autocompact_advisory``). When the env-var combo
    matches the trip condition, the SessionStart handler must append
    the advisory text to ``additionalContext`` so the agent can read
    it the moment the session starts.
    """

    def test_advisory_appended_when_kill_switch_trips(
        self, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture
    ) -> None:
        monkeypatch.setenv("CLAUDE_AUTOCOMPACT_PCT_OVERRIDE", "25")
        monkeypatch.delenv("CLAUDE_CODE_AUTO_COMPACT_WINDOW", raising=False)
        monkeypatch.delenv("DISABLE_COMPACT", raising=False)
        monkeypatch.delenv("DISABLE_AUTO_COMPACT", raising=False)
        monkeypatch.setenv("CLAUDE_CODE_MODEL", "claude-opus-4-7[1m]")

        handle_session_start_bootstrap({"session_id": "s1", "agent_id": "a1"})

        payload = json.loads(capsys.readouterr().out)
        context = payload["hookSpecificOutput"]["additionalContext"]
        assert "AUTO-COMPACT SILENT KILL-SWITCH" in context
        assert "CLAUDE_CODE_AUTO_COMPACT_WINDOW" in context
        assert "1000000" in context
        assert "#980" in context

    def test_no_advisory_when_window_already_set(
        self, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture
    ) -> None:
        # User already has the fix env var in place — must NOT nag.
        monkeypatch.setenv("CLAUDE_AUTOCOMPACT_PCT_OVERRIDE", "25")
        monkeypatch.setenv("CLAUDE_CODE_AUTO_COMPACT_WINDOW", "1000000")
        monkeypatch.setenv("CLAUDE_CODE_MODEL", "claude-opus-4-7[1m]")

        handle_session_start_bootstrap({"session_id": "s2", "agent_id": "a2"})

        payload = json.loads(capsys.readouterr().out)
        assert "AUTO-COMPACT SILENT KILL-SWITCH" not in payload["hookSpecificOutput"]["additionalContext"]

    def test_no_advisory_when_pct_override_unset(
        self, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture
    ) -> None:
        # No user-expressed threshold → kill-switch doesn't matter to user.
        monkeypatch.delenv("CLAUDE_AUTOCOMPACT_PCT_OVERRIDE", raising=False)
        monkeypatch.delenv("CLAUDE_CODE_AUTO_COMPACT_WINDOW", raising=False)
        monkeypatch.setenv("CLAUDE_CODE_MODEL", "claude-opus-4-7[1m]")

        handle_session_start_bootstrap({"session_id": "s3", "agent_id": "a3"})

        payload = json.loads(capsys.readouterr().out)
        assert "AUTO-COMPACT SILENT KILL-SWITCH" not in payload["hookSpecificOutput"]["additionalContext"]
