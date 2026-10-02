"""Tests for the PreCompact auto-snapshot hook (issue #778).

A background sub-agent that auto-compacts WITHOUT having run /t3:retro must
still recover its identity and assignment after compaction. The PreCompact
hook must therefore write a deterministic snapshot from DURABLE state alone
(loop registry, per-session active-repos / loaded-skills) — no dependence on
the agent having behaviorally written a snapshot first. Recovery happens on
the only post-compaction event the harness reads (issue #845): SessionStart
with ``source == "compact"``.
"""

import json
import os
from pathlib import Path

import pytest

import hooks.scripts.hook_router as router
from hooks.scripts.hook_router import (
    _OWNER_LOOP,
    _T3_TEMP_PREFIX,
    _write_loop_registry,
    handle_pre_compact,
    handle_session_start_bootstrap,
)


@pytest.fixture(autouse=True)
def _isolation(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Point STATE_DIR, _TMP_DIR and the loop registry at temp dirs."""
    router.STATE_DIR = tmp_path / "state"
    router._TMP_DIR = tmp_path / "tmp"
    router.STATE_DIR.mkdir(parents=True, exist_ok=True)
    router._TMP_DIR.mkdir(parents=True, exist_ok=True)
    reg_dir = tmp_path / "data"
    reg_dir.mkdir(parents=True, exist_ok=True)
    monkeypatch.setenv("T3_LOOP_REGISTRY_DIR", str(reg_dir))
    monkeypatch.setattr(router, "_TTY_PATH", str(tmp_path / "fake-tty"))
    # Force the teatree opt-in marker AND the #256 auto-load opt-in active:
    # these cover the snapshot / compact-recovery mechanism, not the opt-in gates.
    monkeypatch.setattr(router, "_teatree_active", lambda session_id: True)
    monkeypatch.setattr(router, "_autoload_enabled", lambda: True)


def _snapshot_for(session_id: str) -> Path:
    return router.STATE_DIR / f"{_T3_TEMP_PREFIX}{session_id}-precompact.md"


class TestPreCompactSnapshotFromDurableState:
    def test_subagent_session_gets_snapshot_from_loop_registry(self) -> None:
        session_id = "subagent-loop-sess"
        _write_loop_registry(
            {
                _OWNER_LOOP: {
                    "session_id": session_id,
                    "agent_id": "agent-abc-123",
                    "pid": os.getppid(),
                    "heartbeat_ts": 1,
                }
            }
        )

        handle_pre_compact({"session_id": session_id})

        snapshot = _snapshot_for(session_id)
        assert snapshot.is_file()
        body = snapshot.read_text(encoding="utf-8")
        assert "agent-abc-123" in body
        # #786 WS3: tick-owner snapshot — no roster name, no spawn brief.
        assert "loop OWNER" in body
        assert "t3 loops tick" in body
        assert "t3 loop claim-next" in body

    def test_snapshot_does_not_consume_spawn_brief(self) -> None:
        """#786 WS3 regression: the snapshot must NOT read/emit spawn_brief.

        The tick-owner record has no brief; the old PreCompact branch
        emitted ``- Brief: …`` and was permanently-dead post-WS3. Even if
        a stale brief somehow lingers in a record, the snapshot must
        ignore it (no roster vocabulary, no ``Brief:`` line). RED before
        the dead-branch removal (it surfaced the brief); GREEN after.
        """
        session_id = "stale-brief-sess"
        _write_loop_registry(
            {
                _OWNER_LOOP: {
                    "session_id": session_id,
                    "agent_id": "agent-x",
                    "pid": os.getppid(),
                    "heartbeat_ts": 1,
                    "spawn_brief": "STALE-BRIEF-SENTINEL should never appear",
                }
            }
        )

        handle_pre_compact({"session_id": session_id})

        body = _snapshot_for(session_id).read_text(encoding="utf-8")
        assert "STALE-BRIEF-SENTINEL" not in body
        assert "Brief:" not in body
        assert "singletons" not in body  # retired vocabulary

    def test_snapshot_includes_active_repos_and_skills(self) -> None:
        session_id = "subagent-ctx-sess"
        _write_loop_registry(
            {
                _OWNER_LOOP: {
                    "session_id": session_id,
                    "agent_id": "rev-1",
                    "pid": os.getppid(),
                    "heartbeat_ts": 1,
                }
            }
        )
        (router.STATE_DIR / f"{session_id}.active").write_text("souliane/teatree\n", encoding="utf-8")
        (router.STATE_DIR / f"{session_id}.skills").write_text("t3:code\nt3:review\n", encoding="utf-8")

        handle_pre_compact({"session_id": session_id})

        body = _snapshot_for(session_id).read_text(encoding="utf-8")
        assert "souliane/teatree" in body
        assert "t3:code" in body
        assert "t3:review" in body

    def test_snapshot_written_without_agent_having_run_retro(self) -> None:
        """No prior t3-snapshot file exists — the hook must create one itself."""
        session_id = "no-retro-sess"
        _write_loop_registry(
            {
                _OWNER_LOOP: {
                    "session_id": session_id,
                    "agent_id": "bh-9",
                    "pid": os.getppid(),
                    "heartbeat_ts": 1,
                }
            }
        )
        assert not any(router.STATE_DIR.glob(f"{_T3_TEMP_PREFIX}*.md"))

        handle_pre_compact({"session_id": session_id})

        assert _snapshot_for(session_id).is_file()

    def test_no_session_id_writes_nothing(self) -> None:
        handle_pre_compact({"session_id": ""})
        assert not any(router.STATE_DIR.glob(f"{_T3_TEMP_PREFIX}*.md"))

    def test_session_not_in_registry_still_snapshots_session_id(self) -> None:
        session_id = "lone-subagent"
        (router.STATE_DIR / f"{session_id}.active").write_text("souliane/teatree\n", encoding="utf-8")

        handle_pre_compact({"session_id": session_id})

        snapshot = _snapshot_for(session_id)
        assert snapshot.is_file()
        assert session_id in snapshot.read_text(encoding="utf-8")


class TestSessionStartUsesHookSpecificOutputEnvelope:
    """#1452: SessionStart hook output MUST be nested under ``hookSpecificOutput``.

    The Claude Code harness silently drops the legacy flat top-level
    ``{"additionalContext": ...}`` form for SessionStart events; the
    documented schema (Agent SDK ``SessionStartHookSpecificOutput``)
    requires ``{"hookSpecificOutput": {"hookEventName": "SessionStart",
    "additionalContext": ...}}``. Empirical evidence: 24 compactions in
    session ``a1e3d2d8-…`` emitted the recovery payload via the flat form
    and zero of them resulted in the snapshot text reaching the model.
    """

    def test_compact_recovery_emits_nested_envelope(self, capsys: pytest.CaptureFixture[str]) -> None:
        session_id = "envelope-compact"
        _write_loop_registry(
            {
                _OWNER_LOOP: {
                    "session_id": session_id,
                    "agent_id": "agent-env-1",
                    "pid": os.getppid(),
                    "heartbeat_ts": 1,
                }
            }
        )

        handle_pre_compact({"session_id": session_id})
        capsys.readouterr()
        handle_session_start_bootstrap({"session_id": session_id, "source": "compact"})

        output = json.loads(capsys.readouterr().out)
        assert "additionalContext" not in output, "flat top-level form is silently dropped by the harness"
        assert "hookSpecificOutput" in output
        hook_specific = output["hookSpecificOutput"]
        assert hook_specific["hookEventName"] == "SessionStart"
        assert "additionalContext" in hook_specific

    def test_non_compact_session_start_also_uses_nested_envelope(self, capsys: pytest.CaptureFixture[str]) -> None:
        """Same schema applies to the regular SessionStart path, not just source=compact."""
        handle_session_start_bootstrap({"session_id": "envelope-fresh", "source": "startup"})

        output = json.loads(capsys.readouterr().out)
        assert "additionalContext" not in output
        assert output["hookSpecificOutput"]["hookEventName"] == "SessionStart"
        assert "additionalContext" in output["hookSpecificOutput"]


class TestPreCompactSessionStartRoundTrip:
    def test_sessionstart_compact_reinjects_the_precompact_snapshot_for_subagent(
        self, capsys: pytest.CaptureFixture[str]
    ) -> None:
        session_id = "roundtrip-subagent"
        _write_loop_registry(
            {
                _OWNER_LOOP: {
                    "session_id": session_id,
                    "agent_id": "xrev-7",
                    "pid": os.getppid(),
                    "heartbeat_ts": 1,
                }
            }
        )

        handle_pre_compact({"session_id": session_id})
        capsys.readouterr()  # discard PreCompact's own stdout
        handle_session_start_bootstrap({"session_id": session_id, "source": "compact"})

        output = json.loads(capsys.readouterr().out)
        # #1452: recovery context lives under hookSpecificOutput, not at top level.
        ctx = output["hookSpecificOutput"]["additionalContext"]
        assert "xrev-7" in ctx
        assert "loop OWNER" in ctx
        assert "PRE-COMPACTION SNAPSHOTS RECOVERED" in ctx


class TestMainSessionRetroPathUnaffected:
    def test_lifecycle_skill_session_still_gets_retro_directive(self, capsys: pytest.CaptureFixture[str]) -> None:
        session_id = "main-session"
        (router.STATE_DIR / f"{session_id}.skills").write_text("t3:code\n", encoding="utf-8")

        handle_pre_compact({"session_id": session_id})

        output = json.loads(capsys.readouterr().out)
        assert "additionalContext" in output
        assert "/t3:retro" in output["additionalContext"]
