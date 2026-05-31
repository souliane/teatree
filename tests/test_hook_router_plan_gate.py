"""Tests for the PreToolUse plan-gate hook (#1133).

The gate denies ``Edit``/``Write`` on files under ``$T3_WORKSPACE_DIR``
when **all** of the following hold for the current session:

1. The file path is under ``$T3_WORKSPACE_DIR`` (env var; default ``~/workspace``).
2. No ``/plan`` invocation has been recorded for the session.
3. No source-read of the touched file has been recorded for the session.

The gate is opt-in per overlay via ``[overlays.<name>] plan_gate = true``
in ``~/.teatree.toml``. Default OFF — if no overlay opts in, the handler
returns ``False`` (pass through) and the gate is silent.

Outside ``$T3_WORKSPACE_DIR`` (e.g. ``~/.zshrc``, ``~/.claude/``, the
memory file) the gate never fires.

Integration-style: the real handler, real ``STATE_DIR`` on ``tmp_path``,
real ``~/.teatree.toml`` loaded from ``HOME=tmp_path/home``.
"""

import json
from collections.abc import Iterator
from pathlib import Path

import pytest

import hooks.scripts.hook_router as router
from hooks.scripts.hook_router import (
    handle_enforce_plan_gate,
    handle_track_plan_invocation,
    handle_track_workspace_source_read,
)


@pytest.fixture
def ws(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[Path]:
    """Point ``STATE_DIR`` and ``$T3_WORKSPACE_DIR`` at temp paths.

    The plan-gate consults ``~/.teatree.toml`` to find any overlay with
    ``plan_gate = true``; the autouse ``_isolate_env`` fixture in
    ``conftest.py`` already sets ``HOME`` to a temp directory but does
    not seed ``~/.teatree.toml``, so each test writes its own toml.

    Returns the absolute path to the workspace root. The fixture is not
    autouse: tests that need the gate's scope opt in by requesting ``ws``.
    """
    original_state = router.STATE_DIR
    router.STATE_DIR = tmp_path / "state"
    router.STATE_DIR.mkdir(parents=True, exist_ok=True)

    ws_root = tmp_path / "home" / "workspace"
    ws_root.mkdir(parents=True, exist_ok=True)
    monkeypatch.setenv("T3_WORKSPACE_DIR", str(ws_root))

    yield ws_root

    router.STATE_DIR = original_state


def _write_teatree_toml(*, plan_gate: bool) -> None:
    """Write a ``~/.teatree.toml`` whose single overlay has the given plan_gate.

    HOME is already temp-isolated by ``conftest._isolate_env``.
    """
    home = Path.home()
    home.mkdir(parents=True, exist_ok=True)
    body = "[overlays.t3-teatree]\nplan_gate = " + ("true" if plan_gate else "false") + "\n"
    (home / ".teatree.toml").write_text(body, encoding="utf-8")


def _edit(file_path: str) -> dict:
    return {
        "session_id": "sess-1",
        "tool_name": "Edit",
        "tool_input": {"file_path": file_path, "old_string": "a", "new_string": "b"},
    }


def _write(file_path: str) -> dict:
    return {
        "session_id": "sess-1",
        "tool_name": "Write",
        "tool_input": {"file_path": file_path, "content": "x"},
    }


def _read(file_path: str) -> dict:
    return {
        "session_id": "sess-1",
        "tool_name": "Read",
        "tool_input": {"file_path": file_path},
    }


def _record_plan() -> dict:
    return {
        "session_id": "sess-1",
        "tool_name": "Skill",
        "tool_input": {"skill": "t3:teatree-plan"},
    }


# ── Gate opt-in resolution ───────────────────────────────────────────────


class TestOptIn:
    """Gate is silent unless at least one overlay has plan_gate=true."""

    def test_no_teatree_toml_is_passthrough(self, ws: Path, capsys) -> None:
        target = ws / "myrepo" / "foo.py"
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text("x = 1\n", encoding="utf-8")

        blocked = handle_enforce_plan_gate(_edit(str(target)))

        assert blocked is False
        assert capsys.readouterr().out == ""

    def test_plan_gate_false_is_passthrough(self, ws: Path) -> None:
        _write_teatree_toml(plan_gate=False)
        target = ws / "myrepo" / "foo.py"
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text("x = 1\n", encoding="utf-8")

        assert handle_enforce_plan_gate(_edit(str(target))) is False


# ── Gate active: deny / allow paths ──────────────────────────────────────


class TestGateActive:
    """When ``plan_gate=true`` for any overlay, the gate enforces the rules."""

    def test_edit_in_workspace_with_no_plan_no_read_is_denied(self, ws: Path, capsys) -> None:
        _write_teatree_toml(plan_gate=True)
        target = ws / "myrepo" / "foo.py"
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text("x = 1\n", encoding="utf-8")

        blocked = handle_enforce_plan_gate(_edit(str(target)))

        assert blocked is True
        out = json.loads(capsys.readouterr().out)
        assert out["permissionDecision"] == "deny"
        reason = out["permissionDecisionReason"]
        assert "plan" in reason.lower()
        # Must name the rule and the missing prerequisite.
        assert "$T3_WORKSPACE_DIR" in reason or "T3_WORKSPACE_DIR" in reason
        assert "foo.py" in reason

    def test_edit_after_plan_invocation_is_allowed(self, ws: Path) -> None:
        _write_teatree_toml(plan_gate=True)
        target = ws / "myrepo" / "foo.py"
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text("x = 1\n", encoding="utf-8")

        # PostToolUse: agent invoked the /plan skill earlier in the turn.
        handle_track_plan_invocation(_record_plan())

        assert handle_enforce_plan_gate(_edit(str(target))) is False

    def test_edit_after_source_read_is_allowed(self, ws: Path) -> None:
        _write_teatree_toml(plan_gate=True)
        target = ws / "myrepo" / "foo.py"
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text("x = 1\n", encoding="utf-8")

        # PostToolUse: agent Read the file earlier.
        handle_track_workspace_source_read(_read(str(target)))

        assert handle_enforce_plan_gate(_edit(str(target))) is False

    def test_write_same_as_edit(self, ws: Path, capsys) -> None:
        _write_teatree_toml(plan_gate=True)
        target = ws / "myrepo" / "new_file.py"
        target.parent.mkdir(parents=True, exist_ok=True)

        blocked = handle_enforce_plan_gate(_write(str(target)))

        assert blocked is True
        out = json.loads(capsys.readouterr().out)
        assert out["permissionDecision"] == "deny"

    def test_write_after_plan_is_allowed(self, ws: Path) -> None:
        _write_teatree_toml(plan_gate=True)
        target = ws / "myrepo" / "new_file.py"
        target.parent.mkdir(parents=True, exist_ok=True)

        handle_track_plan_invocation(_record_plan())

        assert handle_enforce_plan_gate(_write(str(target))) is False


# ── Scope: outside the workspace is always passthrough ───────────────────


class TestScope:
    """Edits outside $T3_WORKSPACE_DIR are never affected by the gate."""

    def test_zshrc_outside_workspace_is_passthrough(self) -> None:
        _write_teatree_toml(plan_gate=True)
        # ~/.zshrc lives under HOME but OUTSIDE HOME/workspace.
        target = Path.home() / ".zshrc"
        target.write_text("# alias gs='git status'\n", encoding="utf-8")

        assert handle_enforce_plan_gate(_edit(str(target))) is False

    def test_claude_settings_outside_workspace_is_passthrough(self) -> None:
        _write_teatree_toml(plan_gate=True)
        target = Path.home() / ".claude" / "settings.json"
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text("{}\n", encoding="utf-8")

        assert handle_enforce_plan_gate(_edit(str(target))) is False

    def test_memory_md_outside_workspace_is_passthrough(self) -> None:
        _write_teatree_toml(plan_gate=True)
        target = Path.home() / ".claude" / "projects" / "memory" / "MEMORY.md"
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text("# Memory\n", encoding="utf-8")

        assert handle_enforce_plan_gate(_edit(str(target))) is False


# ── Tool scope: Bash/Read are not affected ───────────────────────────────


class TestToolScope:
    """Only Edit/Write trigger the gate. Bash and Read pass through."""

    def test_bash_in_workspace_is_passthrough(self, ws: Path) -> None:
        _write_teatree_toml(plan_gate=True)
        data = {
            "session_id": "sess-1",
            "tool_name": "Bash",
            "tool_input": {"command": f"ls {ws}/myrepo"},
        }
        assert handle_enforce_plan_gate(data) is False

    def test_read_in_workspace_is_passthrough(self, ws: Path) -> None:
        _write_teatree_toml(plan_gate=True)
        target = ws / "myrepo" / "foo.py"
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text("x = 1\n", encoding="utf-8")
        # Read is what records the source-read; it must never be blocked
        # by the plan-gate itself.
        assert handle_enforce_plan_gate(_read(str(target))) is False


# ── Session scope: state is per-session ──────────────────────────────────


class TestSessionScope:
    """Plan/read records for session A do not authorize session B."""

    def test_plan_in_session_a_does_not_authorize_session_b(self, ws: Path, capsys) -> None:
        _write_teatree_toml(plan_gate=True)
        target = ws / "myrepo" / "foo.py"
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text("x = 1\n", encoding="utf-8")

        # Plan in session A.
        handle_track_plan_invocation(
            {"session_id": "sess-A", "tool_name": "Skill", "tool_input": {"skill": "t3:teatree-plan"}}
        )

        # Edit attempt in session B — no plan, no read for session B.
        data_b = {
            "session_id": "sess-B",
            "tool_name": "Edit",
            "tool_input": {"file_path": str(target), "old_string": "a", "new_string": "b"},
        }
        blocked = handle_enforce_plan_gate(data_b)
        assert blocked is True
        out = json.loads(capsys.readouterr().out)
        assert out["permissionDecision"] == "deny"


# ── Read tracking: only workspace files are recorded ─────────────────────


class TestSourceReadTracking:
    """Reads outside the workspace must not authorize workspace edits."""

    def test_outside_read_does_not_authorize_workspace_edit(self, ws: Path, capsys) -> None:
        _write_teatree_toml(plan_gate=True)
        outside = Path.home() / ".zshrc"
        outside.write_text("# x\n", encoding="utf-8")
        # Read a file outside the workspace.
        handle_track_workspace_source_read(_read(str(outside)))

        target = ws / "myrepo" / "foo.py"
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text("x = 1\n", encoding="utf-8")
        blocked = handle_enforce_plan_gate(_edit(str(target)))
        assert blocked is True
        # Drain stdout.
        capsys.readouterr()

    def test_read_of_different_workspace_file_does_not_authorize_edit(self, ws: Path, capsys) -> None:
        _write_teatree_toml(plan_gate=True)
        other = ws / "myrepo" / "bar.py"
        other.parent.mkdir(parents=True, exist_ok=True)
        other.write_text("x = 1\n", encoding="utf-8")
        handle_track_workspace_source_read(_read(str(other)))

        target = ws / "myrepo" / "foo.py"
        target.write_text("y = 2\n", encoding="utf-8")
        # Editing a DIFFERENT file — the read of bar.py does not authorize foo.py.
        blocked = handle_enforce_plan_gate(_edit(str(target)))
        assert blocked is True
        capsys.readouterr()
