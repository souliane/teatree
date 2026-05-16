"""Tests for the PreCompact auto-snapshot hook (issue #778).

A background sub-agent that auto-compacts WITHOUT having run /t3:retro must
still recover its identity and assignment after compaction. The PreCompact
hook must therefore write a deterministic snapshot from DURABLE state alone
(loop registry, per-session active-repos / loaded-skills) — no dependence on
the agent having behaviorally written a snapshot first. The PostCompact hook
(already session-agnostic) then re-injects it.
"""

import json
import os
from pathlib import Path

import pytest

import hooks.scripts.hook_router as router
from hooks.scripts.hook_router import _T3_TEMP_PREFIX, _write_loop_registry, handle_post_compact, handle_pre_compact


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


def _snapshot_for(session_id: str) -> Path:
    return router.STATE_DIR / f"{_T3_TEMP_PREFIX}{session_id}-precompact.md"


class TestPreCompactSnapshotFromDurableState:
    def test_subagent_session_gets_snapshot_from_loop_registry(self) -> None:
        session_id = "subagent-loop-sess"
        _write_loop_registry(
            {
                "t3-main-loop": {
                    "session_id": session_id,
                    "agent_id": "agent-abc-123",
                    "pid": os.getppid(),
                    "spawn_brief": "t3-main-loop — drive the backlog to zero, smallest-first.",
                }
            }
        )

        handle_pre_compact({"session_id": session_id})

        snapshot = _snapshot_for(session_id)
        assert snapshot.is_file()
        body = snapshot.read_text(encoding="utf-8")
        assert "agent-abc-123" in body
        assert "t3-main-loop" in body
        assert "drive the backlog to zero" in body

    def test_snapshot_includes_active_repos_and_skills(self) -> None:
        session_id = "subagent-ctx-sess"
        _write_loop_registry(
            {
                "t3-review-loop": {
                    "session_id": session_id,
                    "agent_id": "rev-1",
                    "pid": os.getppid(),
                    "spawn_brief": "t3-review-loop — review every merged PR.",
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
                "t3-bug-hunt": {
                    "session_id": session_id,
                    "agent_id": "bh-9",
                    "pid": os.getppid(),
                    "spawn_brief": "t3-bug-hunt — hunt bugs in core + overlay.",
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


class TestPreCompactPostCompactRoundTrip:
    def test_postcompact_reinjects_the_precompact_snapshot_for_subagent(
        self, capsys: pytest.CaptureFixture[str]
    ) -> None:
        session_id = "roundtrip-subagent"
        _write_loop_registry(
            {
                "t3-cross-review-loop": {
                    "session_id": session_id,
                    "agent_id": "xrev-7",
                    "pid": os.getppid(),
                    "spawn_brief": "t3-cross-review-loop — architectural cross-repo review.",
                }
            }
        )

        handle_pre_compact({"session_id": session_id})
        handle_post_compact({"session_id": session_id})

        output = json.loads(capsys.readouterr().out)
        assert "additionalContext" in output
        ctx = output["additionalContext"]
        assert "xrev-7" in ctx
        assert "t3-cross-review-loop" in ctx
        assert "PRE-COMPACTION SNAPSHOTS RECOVERED" in ctx


class TestMainSessionRetroPathUnaffected:
    def test_lifecycle_skill_session_still_gets_retro_directive(self, capsys: pytest.CaptureFixture[str]) -> None:
        session_id = "main-session"
        (router.STATE_DIR / f"{session_id}.skills").write_text("t3:code\n", encoding="utf-8")

        handle_pre_compact({"session_id": session_id})

        output = json.loads(capsys.readouterr().out)
        assert "additionalContext" in output
        assert "/t3:retro" in output["additionalContext"]
