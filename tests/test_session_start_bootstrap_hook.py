"""Tests for the SessionStart hook handler (singleton loop orchestration bootstrap).

Covers issue #718: a SessionStart hook emits ``additionalContext`` that
idempotently establishes / re-attaches the three machine-wide singleton loop
sub-agents, prints the ``/rename`` reminder only for the loop owner, and
best-effort sets the terminal title via an OSC escape gated on an interactive
TTY + owner-only.
"""

import json
import os
from pathlib import Path

import pytest

import hooks.scripts.hook_router as router
from hooks.scripts.hook_router import (
    _SPAWN_DIRECTIVE,
    LOOP_AGENT_NAMES,
    _loop_registry_path,
    _prune_dead_owner,
    _read_loop_registry,
    _write_loop_registry,
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
    return os.getpid()


class TestLoopRegistry:
    def test_registry_path_under_configured_dir(self, registry_paths) -> None:
        reg_dir, _ = registry_paths
        assert _loop_registry_path() == reg_dir / "loop-registry.json"

    def test_read_missing_registry_returns_empty(self) -> None:
        assert _read_loop_registry() == {}

    def test_write_then_read_roundtrip(self) -> None:
        entry = {"session_id": "s1", "agent_id": "a1", "pid": _live_pid()}
        _write_loop_registry({"teatree-main-loop": entry})
        assert _read_loop_registry() == {"teatree-main-loop": entry}

    def test_read_corrupt_registry_returns_empty(self) -> None:
        _loop_registry_path().write_text("{ not json", encoding="utf-8")
        assert _read_loop_registry() == {}

    def test_prune_removes_dead_owner(self) -> None:
        # PID 999999 is (almost certainly) not alive.
        reg = {"teatree-main-loop": {"session_id": "s1", "agent_id": "a1", "pid": 999999}}
        _write_loop_registry(reg)
        pruned = _prune_dead_owner(_read_loop_registry())
        assert pruned == {}

    def test_prune_keeps_live_owner(self) -> None:
        reg = {"teatree-main-loop": {"session_id": "s1", "agent_id": "a1", "pid": _live_pid()}}
        _write_loop_registry(reg)
        assert _prune_dead_owner(_read_loop_registry()) == reg

    def test_loop_agent_names_are_the_three_singletons(self) -> None:
        assert LOOP_AGENT_NAMES == (
            "teatree-main-loop",
            "teatree-review-loop",
            "teatree-cross-review-loop",
        )


class TestHandleSessionStartBootstrap:
    def test_no_session_id_produces_no_output(self, capsys: pytest.CaptureFixture[str]) -> None:
        handle_session_start_bootstrap({})
        assert capsys.readouterr().out == ""

    def test_fresh_machine_instructs_spawn_and_claims_ownership(self, capsys: pytest.CaptureFixture[str]) -> None:
        handle_session_start_bootstrap({"session_id": "owner-1"})

        out = json.loads(capsys.readouterr().out)
        ctx = out["additionalContext"]
        assert "teatree-main-loop" in ctx
        assert "teatree-review-loop" in ctx
        assert "teatree-cross-review-loop" in ctx
        assert "spawn" in ctx.lower()
        # Owner gets the rename reminder.
        assert "/rename TEATREE LOOP" in ctx
        # Per-ticket -> per-step model is described.
        assert "per ticket" in ctx.lower() or "per-ticket" in ctx.lower()

        reg = _read_loop_registry()
        assert reg["teatree-main-loop"]["session_id"] == "owner-1"
        assert reg["teatree-main-loop"]["pid"] == _live_pid()

    def test_owner_records_agent_id_when_present(self, capsys: pytest.CaptureFixture[str]) -> None:
        handle_session_start_bootstrap({"session_id": "owner-1", "agent_id": "agent-xyz"})
        capsys.readouterr()
        assert _read_loop_registry()["teatree-main-loop"]["agent_id"] == "agent-xyz"

    def test_second_live_session_instructs_reattach_not_spawn(self, capsys: pytest.CaptureFixture[str]) -> None:
        # First session claims ownership with a LIVE pid.
        _write_loop_registry(
            {
                "teatree-main-loop": {
                    "session_id": "owner-1",
                    "agent_id": "agent-owner",
                    "pid": _live_pid(),
                }
            }
        )

        handle_session_start_bootstrap({"session_id": "second-2"})

        ctx = json.loads(capsys.readouterr().out)["additionalContext"]
        assert "re-attach" in ctx.lower() or "reattach" in ctx.lower()
        assert "agent-owner" in ctx  # re-attach by recorded agent id
        # The directive must explicitly forbid a duplicate spawn, never instruct one.
        assert "do not spawn" in ctx.lower()
        # And it must NOT be the spawn directive.
        assert ctx != _SPAWN_DIRECTIVE
        # Non-owner must NOT get the rename reminder.
        assert "/rename TEATREE LOOP" not in ctx
        # Ownership is unchanged.
        assert _read_loop_registry()["teatree-main-loop"]["session_id"] == "owner-1"

    def test_same_session_restart_is_idempotent_still_owner(self, capsys: pytest.CaptureFixture[str]) -> None:
        _write_loop_registry(
            {
                "teatree-main-loop": {
                    "session_id": "owner-1",
                    "agent_id": "agent-owner",
                    "pid": _live_pid(),
                }
            }
        )

        handle_session_start_bootstrap({"session_id": "owner-1"})

        ctx = json.loads(capsys.readouterr().out)["additionalContext"]
        assert "spawn" in ctx.lower()
        assert "/rename TEATREE LOOP" in ctx
        assert _read_loop_registry()["teatree-main-loop"]["session_id"] == "owner-1"

    def test_dead_owner_is_reclaimed_by_new_session(self, capsys: pytest.CaptureFixture[str]) -> None:
        _write_loop_registry(
            {
                "teatree-main-loop": {
                    "session_id": "dead-owner",
                    "agent_id": "ghost",
                    "pid": 999999,
                }
            }
        )

        handle_session_start_bootstrap({"session_id": "new-owner"})

        ctx = json.loads(capsys.readouterr().out)["additionalContext"]
        assert "spawn" in ctx.lower()
        assert _read_loop_registry()["teatree-main-loop"]["session_id"] == "new-owner"

    def test_owner_with_tty_emits_osc_title(self, registry_paths) -> None:
        _, tty_path = registry_paths
        # Make the configured tty sink writable (simulates an interactive TTY).
        Path(tty_path).write_text("", encoding="utf-8")

        handle_session_start_bootstrap({"session_id": "owner-1"})

        written = Path(tty_path).read_text(encoding="utf-8")
        assert "\033]0;TEATREE LOOP\007" in written

    def test_owner_without_tty_does_not_crash_and_skips_osc(
        self, registry_paths, capsys: pytest.CaptureFixture[str]
    ) -> None:
        # tty sink path does not exist -> not an interactive TTY -> no OSC, no crash.
        handle_session_start_bootstrap({"session_id": "owner-1"})
        out = json.loads(capsys.readouterr().out)
        assert "additionalContext" in out  # still emitted the directive

    def test_non_owner_with_tty_does_not_emit_osc(self, registry_paths) -> None:
        _, tty_path = registry_paths
        Path(tty_path).write_text("", encoding="utf-8")
        _write_loop_registry(
            {
                "teatree-main-loop": {
                    "session_id": "owner-1",
                    "agent_id": "agent-owner",
                    "pid": _live_pid(),
                }
            }
        )

        handle_session_start_bootstrap({"session_id": "non-owner"})

        assert Path(tty_path).read_text(encoding="utf-8") == ""


class TestSessionStartWiredIntoRouter:
    def test_session_start_in_handlers_table(self) -> None:
        assert "SessionStart" in router._HANDLERS
        assert handle_session_start_bootstrap in router._HANDLERS["SessionStart"]
