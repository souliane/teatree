"""Tests for #1447 — hook_router.main() must exit 2 on deny + emit nested schema.

Claude Code 2.1.146 silently ignored exit-0 deny payloads from
``PreToolUse`` hooks. The harness now honours **exit code 2** only.
The router's ``main()`` must therefore exit 2 whenever any handler
emits a deny, and 0 otherwise.

Independently, the modern Claude Code SDK schema
(``claude_agent_sdk.types.PreToolUseHookSpecificOutput``) expects the
``permissionDecision`` field nested inside ``hookSpecificOutput``:

    {"hookSpecificOutput": {
        "hookEventName": "PreToolUse",
        "permissionDecision": "deny",
        "permissionDecisionReason": "..."}}

Every deny emit site must produce that nested shape (legacy flat
fields may co-exist for backward-compat with existing consumers, but
the nested form must be present).

These tests are integration-style: ``main()`` is invoked as a
subprocess so the real exit code propagates through ``sys.exit``.
"""

import json
import subprocess
import sys
from pathlib import Path

import pytest

HOOK_ROUTER = Path(__file__).resolve().parent.parent / "hooks" / "scripts" / "hook_router.py"


def _run_router(event: str, payload: dict) -> subprocess.CompletedProcess[str]:
    """Run hook_router.py as a subprocess; return (returncode, stdout, stderr)."""
    return subprocess.run(
        [sys.executable, str(HOOK_ROUTER), "--event", event],
        input=json.dumps(payload),
        capture_output=True,
        text=True,
        check=False,
        timeout=10,
    )


def _transcript(tmp_path: Path, *, sidechain: bool) -> str:
    """Write a one-turn transcript marking the active turn as main/sub agent."""
    entry: dict = {
        "type": "assistant",
        "isSidechain": sidechain,
        "message": {"role": "assistant", "content": []},
    }
    path = tmp_path / "transcript.jsonl"
    path.write_text(json.dumps(entry) + "\n", encoding="utf-8")
    return str(path)


# ── Test 1: deny ⇒ exit code 2 ────────────────────────────────────────


class TestDenyExitsCodeTwo:
    """Deny verdicts must propagate as ``sys.exit(2)`` from ``main()``.

    The orchestrator-boundary handler denies a main-agent ``Write``.
    The router's ``main()`` MUST exit 2 so Claude Code 2.1.146+ honours
    the deny — an exit-0 deny is silently ignored by the harness.
    """

    def test_main_agent_write_denied_exits_2(self, tmp_path: Path) -> None:
        payload = {
            "tool_name": "Write",
            "tool_input": {"file_path": "/x", "content": "y"},
            "transcript_path": _transcript(tmp_path, sidechain=False),
        }
        result = _run_router("PreToolUse", payload)

        assert result.returncode == 2, f"deny must exit 2 (got {result.returncode}); stdout={result.stdout!r}"
        out = json.loads(result.stdout)
        # Some deny shape must be in stdout — assert via nested form
        # (the legacy top-level form may also be present for back-compat).
        assert out["hookSpecificOutput"]["permissionDecision"] == "deny"

    def test_main_agent_edit_denied_exits_2(self, tmp_path: Path) -> None:
        payload = {
            "tool_name": "Edit",
            "tool_input": {"file_path": "/x", "old_string": "a", "new_string": "b"},
            "transcript_path": _transcript(tmp_path, sidechain=False),
        }
        result = _run_router("PreToolUse", payload)
        assert result.returncode == 2


# ── Test 2: allow / no-op ⇒ exit code 0 ───────────────────────────────


class TestAllowOrNoopExitsCodeZero:
    """Payloads no handler denies must keep exit 0 (the fail-open path).

    The orchestrator-boundary handler does NOT deny sub-agent writes or
    main-agent orchestration verbs (``Task``). A registered allow handler
    or no handler firing must leave the router exiting 0.
    """

    def test_subagent_write_not_denied_exits_0(self, tmp_path: Path) -> None:
        payload = {
            "tool_name": "Write",
            "tool_input": {"file_path": "/x", "content": "y"},
            "transcript_path": _transcript(tmp_path, sidechain=True),
        }
        result = _run_router("PreToolUse", payload)
        assert result.returncode == 0, (
            f"sub-agent write must not be denied (got rc={result.returncode}); stdout={result.stdout!r}"
        )

    def test_main_agent_ask_user_question_not_denied_exits_0(self, tmp_path: Path) -> None:
        # AskUserQuestion is a sanctioned orchestration verb and is exempt
        # from both the orchestrator-execution-boundary gate (it's an
        # orchestration action) and the agent-plan-gate (which only
        # targets Agent/Task). No registered PreToolUse handler should
        # deny it for the main agent.
        payload = {
            "tool_name": "AskUserQuestion",
            "tool_input": {"questions": []},
            "transcript_path": _transcript(tmp_path, sidechain=False),
        }
        result = _run_router("PreToolUse", payload)
        assert result.returncode == 0, (
            f"AskUserQuestion must not be denied (got rc={result.returncode}); stdout={result.stdout!r}"
        )

    def test_unknown_event_exits_0(self) -> None:
        # No handlers registered for an unknown event ⇒ silent passthrough.
        result = _run_router("UnknownEvent", {})
        assert result.returncode == 0


# ── Test 3: nested hookSpecificOutput schema for deny ─────────────────


class TestDenyJsonUsesNestedHookSpecificOutputSchema:
    """Every deny site must emit ``hookSpecificOutput.permissionDecision``.

    The modern Claude Code SDK schema (PreToolUseHookSpecificOutput)
    places ``permissionDecision`` inside ``hookSpecificOutput``. The
    legacy flat shape (``{"permissionDecision": "deny", ...}``) works
    today but is at risk of silent regression. The router emits the
    nested shape uniformly via a shared helper so every deny site is
    immune to that drift.
    """

    def test_orchestrator_boundary_deny_is_nested(self, tmp_path: Path) -> None:
        payload = {
            "tool_name": "Write",
            "tool_input": {"file_path": "/x", "content": "y"},
            "transcript_path": _transcript(tmp_path, sidechain=False),
        }
        result = _run_router("PreToolUse", payload)
        out = json.loads(result.stdout)

        nested = out.get("hookSpecificOutput")
        assert nested is not None, "deny must include hookSpecificOutput envelope"
        assert nested.get("hookEventName") == "PreToolUse"
        assert nested.get("permissionDecision") == "deny"
        assert isinstance(nested.get("permissionDecisionReason"), str)
        assert nested["permissionDecisionReason"], "reason must be non-empty"

    def test_orchestrator_boundary_reason_mentions_orchestrator(self, tmp_path: Path) -> None:
        payload = {
            "tool_name": "Edit",
            "tool_input": {"file_path": "/x", "old_string": "a", "new_string": "b"},
            "transcript_path": _transcript(tmp_path, sidechain=False),
        }
        result = _run_router("PreToolUse", payload)
        out = json.loads(result.stdout)
        reason = out["hookSpecificOutput"]["permissionDecisionReason"]
        assert "orchestrator" in reason


# ── Test 4: in-process helper unit tests for the deny emitter ─────────


class TestEmitDenyHelper:
    """The shared emit-deny helper writes the nested schema and returns True.

    A single helper centralises the schema so every deny site emits the
    same modern shape. Adding a new deny gate is then schema-immune.
    """

    def test_helper_emits_nested_schema(self, capsys: pytest.CaptureFixture[str]) -> None:
        from hooks.scripts.hook_router import emit_pretooluse_deny  # noqa: PLC0415

        result = emit_pretooluse_deny("test reason")

        assert result is True
        out = json.loads(capsys.readouterr().out)
        assert out["hookSpecificOutput"]["hookEventName"] == "PreToolUse"
        assert out["hookSpecificOutput"]["permissionDecision"] == "deny"
        assert out["hookSpecificOutput"]["permissionDecisionReason"] == "test reason"

    def test_helper_preserves_legacy_top_level_keys(self, capsys: pytest.CaptureFixture[str]) -> None:
        """Legacy flat-form consumers (existing handler tests) keep working.

        The helper writes BOTH the nested and the legacy flat fields so
        existing tests that read ``out["permissionDecision"]`` stay GREEN
        without a mass-test-update.
        """
        from hooks.scripts.hook_router import emit_pretooluse_deny  # noqa: PLC0415

        emit_pretooluse_deny("legacy compat")
        out = json.loads(capsys.readouterr().out)
        assert out["permissionDecision"] == "deny"
        assert out["permissionDecisionReason"] == "legacy compat"
