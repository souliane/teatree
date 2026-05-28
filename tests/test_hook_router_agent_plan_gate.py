"""Tests for the PreToolUse Agent-dispatch plan-gate hook (#1302).

The gate denies ``Agent`` / ``Task`` tool dispatch unless **one** of:

1. The current session has a recent ``/plan`` invocation, recorded as a
    POSIX timestamp in ``~/.local/share/teatree/last-plan-skill-ts``
    (within ``TEATREE_PLAN_GATE_WINDOW_MINUTES`` of now, default ``30``).
2. The Agent ``prompt`` carries an explicit per-call opt-out token
    ``[skip-plan-gate: <reason>]`` near the start of the prompt.

Sibling enforcement gate to ``handle_enforce_plan_gate`` (which guards
``Edit``/``Write`` under ``$T3_WORKSPACE_DIR``). This one guards the
dispatch step itself — the orchestrator's ``Agent`` calls — so a missing
``/plan`` pass is caught before a sub-agent burns cycles on a brief
written from stale memory.

Integration-style: the real handler, the real ``STATE_DIR`` on
``tmp_path``, the real timestamp file under ``$XDG_DATA_HOME/teatree``.
"""

import json
import time
from collections.abc import Iterator
from pathlib import Path

import pytest

import hooks.scripts.hook_router as router
from hooks.scripts.hook_router import handle_enforce_agent_plan_gate, handle_track_plan_skill_timestamp


@pytest.fixture
def gate_env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[Path]:
    """Isolate STATE_DIR + XDG_DATA_HOME so the gate's marker is per-test.

    The conftest ``_isolate_env`` autouse fixture already redirects HOME
    to a tmp dir. This fixture additionally pins XDG_DATA_HOME so the
    timestamp file at ``$XDG_DATA_HOME/teatree/last-plan-skill-ts`` is
    isolated, and points STATE_DIR at a fresh tmp directory. Yields the
    resolved path to the timestamp file.
    """
    original_state = router.STATE_DIR
    router.STATE_DIR = tmp_path / "state"
    router.STATE_DIR.mkdir(parents=True, exist_ok=True)

    xdg = tmp_path / "xdg-data"
    xdg.mkdir(parents=True, exist_ok=True)
    monkeypatch.setenv("XDG_DATA_HOME", str(xdg))
    monkeypatch.delenv("TEATREE_PLAN_GATE_WINDOW_MINUTES", raising=False)

    yield xdg / "teatree" / "last-plan-skill-ts"

    router.STATE_DIR = original_state


def _agent(prompt: str, *, subagent_type: str = "t3:coder") -> dict:
    return {
        "session_id": "sess-1",
        "tool_name": "Agent",
        "tool_input": {
            "description": "implement feature",
            "prompt": prompt,
            "subagent_type": subagent_type,
        },
    }


def _record_plan_skill() -> dict:
    return {
        "session_id": "sess-1",
        "tool_name": "Skill",
        "tool_input": {"skill": "plan"},
    }


def _write_ts(ts_file: Path, ts: float) -> None:
    """Write a POSIX timestamp into the gate's marker file."""
    ts_file.parent.mkdir(parents=True, exist_ok=True)
    ts_file.write_text(str(int(ts)), encoding="utf-8")


# ── Block by default ─────────────────────────────────────────────────────


class TestBlockedByDefault:
    """Agent dispatch with no plan timestamp is denied."""

    def test_agent_with_no_plan_ts_is_denied(self, gate_env: Path, capsys) -> None:
        blocked = handle_enforce_agent_plan_gate(_agent("implement feature X"))

        assert blocked is True
        out = json.loads(capsys.readouterr().out)
        assert out["permissionDecision"] == "deny"
        reason = out["permissionDecisionReason"]
        # Reason must name the gate, the unblock paths, and the skip token shape.
        assert "/plan" in reason
        assert "skip-plan-gate" in reason

    def test_task_tool_treated_same_as_agent(self, gate_env: Path, capsys) -> None:
        data = {
            "session_id": "sess-1",
            "tool_name": "Task",
            "tool_input": {"description": "research", "prompt": "look into X", "subagent_type": "t3:coder"},
        }
        blocked = handle_enforce_agent_plan_gate(data)
        assert blocked is True
        out = json.loads(capsys.readouterr().out)
        assert out["permissionDecision"] == "deny"


# ── Skip token bypass ────────────────────────────────────────────────────


class TestSkipToken:
    """An explicit ``[skip-plan-gate: <reason>]`` token unblocks the call."""

    def test_skip_token_at_start_allows_dispatch(self, gate_env: Path) -> None:
        prompt = "[skip-plan-gate: trivial-bug-fix]\n\nFix the typo in foo.py"
        assert handle_enforce_agent_plan_gate(_agent(prompt)) is False

    def test_skip_token_inline_first_line_allows_dispatch(self, gate_env: Path) -> None:
        prompt = "[skip-plan-gate: hotfix] Push the one-char fix"
        assert handle_enforce_agent_plan_gate(_agent(prompt)) is False

    def test_skip_token_requires_reason(self, gate_env: Path, capsys) -> None:
        # Empty reason ⇒ token is malformed ⇒ does NOT unblock.
        prompt = "[skip-plan-gate: ]\n\nDo something"
        blocked = handle_enforce_agent_plan_gate(_agent(prompt))
        assert blocked is True
        capsys.readouterr()


# ── Cooldown window ──────────────────────────────────────────────────────


class TestCooldownWindow:
    """A fresh plan timestamp allows; a stale one (past window) blocks."""

    def test_fresh_plan_ts_within_window_allows(self, gate_env: Path) -> None:
        _write_ts(gate_env, time.time() - 5 * 60)  # 5 min ago
        assert handle_enforce_agent_plan_gate(_agent("implement X")) is False

    def test_stale_plan_ts_past_default_window_blocks(self, gate_env: Path, capsys) -> None:
        _write_ts(gate_env, time.time() - 35 * 60)  # 35 min ago (past 30-min default)
        blocked = handle_enforce_agent_plan_gate(_agent("implement X"))
        assert blocked is True
        capsys.readouterr()

    def test_custom_window_via_env_var(self, gate_env: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("TEATREE_PLAN_GATE_WINDOW_MINUTES", "60")
        # 45 min ago: stale under 30-min default, fresh under 60-min override.
        _write_ts(gate_env, time.time() - 45 * 60)
        assert handle_enforce_agent_plan_gate(_agent("implement X")) is False


# ── PostToolUse: timestamp writer ────────────────────────────────────────


class TestPlanTimestampTracking:
    """``handle_track_plan_skill_timestamp`` writes the gate's marker."""

    def test_plan_skill_invocation_writes_timestamp(self, gate_env: Path) -> None:
        before = int(time.time())
        handle_track_plan_skill_timestamp(_record_plan_skill())

        assert gate_env.is_file()
        ts = int(gate_env.read_text(encoding="utf-8").strip())
        # The recorded timestamp must be a recent POSIX time, not zero
        # or a stale leftover.
        assert ts >= before
        assert ts <= int(time.time()) + 1

    def test_non_plan_skill_does_not_write_timestamp(self, gate_env: Path) -> None:
        data = {
            "session_id": "sess-1",
            "tool_name": "Skill",
            "tool_input": {"skill": "t3:code"},
        }
        handle_track_plan_skill_timestamp(data)
        assert not gate_env.is_file()

    def test_plan_variant_names_count_as_plan(self, gate_env: Path) -> None:
        # ``t3:plan``, ``plan-something``, etc. all count.
        for skill in ("t3:plan", "plan", "plan-feature"):
            gate_env.unlink(missing_ok=True)
            data = {"session_id": "s", "tool_name": "Skill", "tool_input": {"skill": skill}}
            handle_track_plan_skill_timestamp(data)
            assert gate_env.is_file(), f"timestamp should be written for skill={skill!r}"

    def test_end_to_end_plan_then_agent_dispatch_allowed(self, gate_env: Path) -> None:
        # Record a plan invocation, then dispatch — must pass.
        handle_track_plan_skill_timestamp(_record_plan_skill())
        assert handle_enforce_agent_plan_gate(_agent("implement X")) is False


# ── Out of scope: non-Agent tools ────────────────────────────────────────


class TestToolScope:
    """Only Agent/Task tools trigger the gate."""

    @pytest.mark.parametrize("tool_name", ["Bash", "Edit", "Write", "Read", "Grep", "AskUserQuestion"])
    def test_other_tools_pass_through(self, gate_env: Path, tool_name: str) -> None:
        data = {"session_id": "sess-1", "tool_name": tool_name, "tool_input": {}}
        assert handle_enforce_agent_plan_gate(data) is False
