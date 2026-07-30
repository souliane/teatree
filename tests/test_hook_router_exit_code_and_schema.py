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
import os
import sqlite3
import subprocess
import sys
import tempfile
from pathlib import Path

import pytest

HOOK_ROUTER = Path(__file__).resolve().parent.parent / "hooks" / "scripts" / "hook_router.py"


def _seed_config_db(path: Path, rows: dict[str, object]) -> None:
    """Seed the DB-home ``teatree_config_setting`` store the cold hook readers resolve."""
    conn = sqlite3.connect(str(path))
    try:
        conn.execute(
            "CREATE TABLE IF NOT EXISTS teatree_config_setting "
            "(id INTEGER PRIMARY KEY, scope TEXT NOT NULL DEFAULT '', key TEXT NOT NULL, value TEXT NOT NULL)"
        )
        for key, value in rows.items():
            conn.execute(
                "INSERT INTO teatree_config_setting (scope, key, value) VALUES ('', ?, ?)",
                (key, json.dumps(value)),
            )
        conn.commit()
    finally:
        conn.close()


def _run_router(
    event: str, payload: dict, *, settings: dict[str, object] | None = None
) -> subprocess.CompletedProcess[str]:
    """Run hook_router.py as a subprocess; return (returncode, stdout, stderr).

    Runs with ``HOME`` at a clean temp dir and no ``T3_CONFIG_DB`` /
    ``XDG_DATA_HOME`` so the orchestrator-Bash gate's DB-home config read
    resolves to an absent store and sees its default (enabled) — isolating
    these deny/allow assertions from the developer's real config (which may set
    the #115 failsafe to disable the gate).

    ``settings`` seeds the DB-home ``teatree_config_setting`` store (pointed to
    via ``T3_CONFIG_DB``) — used to enable a default-OFF gate (e.g. the #1442
    foreground-Agent deny) whose deny path this contract test must exercise.
    """
    with tempfile.TemporaryDirectory() as home:
        env = {**os.environ, "HOME": home, "USERPROFILE": home}
        env.pop("XDG_DATA_HOME", None)
        if settings is not None:
            db = Path(home) / "db.sqlite3"
            _seed_config_db(db, settings)
            env["T3_CONFIG_DB"] = str(db)
        else:
            env.pop("T3_CONFIG_DB", None)
        return subprocess.run(
            [sys.executable, str(HOOK_ROUTER), "--event", event],
            input=json.dumps(payload),
            capture_output=True,
            text=True,
            check=False,
            timeout=30,
            env=env,
        )


# A main-agent heavy-Bash payload is the orchestrator-boundary gate's
# deny trigger (#115): no ``agent_id`` (main agent) + a heavy command +
# foreground. Sub-agent calls carry a non-empty ``agent_id`` and pass.
def _main_agent_heavy_bash() -> dict:
    return {"tool_name": "Bash", "tool_input": {"command": "uv run pytest --no-cov -q"}}


def _subagent_heavy_bash() -> dict:
    return {
        "tool_name": "Bash",
        "tool_input": {"command": "uv run pytest --no-cov -q"},
        "agent_id": "a4ad83956ff699aaa",
        "agent_type": "general-purpose",
    }


# ── Test 1: deny ⇒ exit code 2 ────────────────────────────────────────


class TestDenyExitsCodeTwo:
    """Deny verdicts must propagate as ``sys.exit(2)`` from ``main()``.

    The orchestrator-boundary handler denies a main-agent heavy Bash
    command. The router's ``main()`` MUST exit 2 so Claude Code 2.1.146+
    honours the deny — an exit-0 deny is silently ignored by the harness.
    """

    def test_main_agent_heavy_bash_denied_exits_2(self) -> None:
        result = _run_router("PreToolUse", _main_agent_heavy_bash())

        assert result.returncode == 2, f"deny must exit 2 (got {result.returncode}); stdout={result.stdout!r}"
        out = json.loads(result.stdout)
        # Some deny shape must be in stdout — assert via nested form
        # (the legacy top-level form may also be present for back-compat).
        assert out["hookSpecificOutput"]["permissionDecision"] == "deny"

    def test_main_agent_foreground_agent_denied_exits_2(self) -> None:
        # The #1442 foreground-Agent deny ships default-OFF (lockout risk on
        # the orchestrator's own hot path), so the deny path must be enabled
        # via config to exercise the deny-exits-2 contract on the Agent arm.
        payload = {"tool_name": "Agent", "tool_input": {"description": "x", "run_in_background": False}}
        settings = {"orchestrator_boundary_agent_gate_enabled": True}
        result = _run_router("PreToolUse", payload, settings=settings)
        assert result.returncode == 2, f"deny must exit 2 (got {result.returncode}); stdout={result.stdout!r}"
        out = json.loads(result.stdout)
        assert out["hookSpecificOutput"]["permissionDecision"] == "deny"


# ── Test 2: allow / no-op ⇒ exit code 0 ───────────────────────────────


class TestAllowOrNoopExitsCodeZero:
    """Payloads no handler denies must keep exit 0 (the fail-open path).

    The orchestrator-boundary handler does NOT deny sub-agent Bash,
    main-agent quick Bash, or main-agent orchestration verbs. A
    registered allow handler or no handler firing must leave the router
    exiting 0.
    """

    def test_subagent_heavy_bash_not_denied_exits_0(self) -> None:
        result = _run_router("PreToolUse", _subagent_heavy_bash())
        assert result.returncode == 0, (
            f"sub-agent heavy Bash must not be denied (got rc={result.returncode}); stdout={result.stdout!r}"
        )

    def test_main_agent_quick_bash_not_denied_exits_0(self) -> None:
        payload = {"tool_name": "Bash", "tool_input": {"command": "git status"}}
        result = _run_router("PreToolUse", payload)
        assert result.returncode == 0, (
            f"main-agent quick Bash must not be denied (got rc={result.returncode}); stdout={result.stdout!r}"
        )

    def test_main_agent_ask_user_question_not_denied_exits_0(self) -> None:
        # AskUserQuestion is a sanctioned orchestration verb and is exempt
        # from both the orchestrator-execution-boundary gate (it's an
        # orchestration action) and the agent-plan-gate (which only
        # targets Agent/Task). No registered PreToolUse handler should
        # deny it for the main agent.
        payload = {"tool_name": "AskUserQuestion", "tool_input": {"questions": []}}
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

    def test_orchestrator_boundary_deny_is_nested(self) -> None:
        result = _run_router("PreToolUse", _main_agent_heavy_bash())
        out = json.loads(result.stdout)

        nested = out.get("hookSpecificOutput")
        assert nested is not None, "deny must include hookSpecificOutput envelope"
        assert nested.get("hookEventName") == "PreToolUse"
        assert nested.get("permissionDecision") == "deny"
        assert isinstance(nested.get("permissionDecisionReason"), str)
        assert nested["permissionDecisionReason"], "reason must be non-empty"

    def test_orchestrator_boundary_reason_mentions_orchestrator(self) -> None:
        result = _run_router("PreToolUse", _main_agent_heavy_bash())
        out = json.loads(result.stdout)
        reason = out["hookSpecificOutput"]["permissionDecisionReason"]
        assert "orchestrator" in reason


# ── Stop / SubagentStop blocks exit 0 with stdout JSON (#1764) ─────────


def _stop_block_transcript(home: str) -> str:
    """Write a transcript whose final assistant turn poses a user-directed question.

    Triggers ``handle_enforce_structured_question`` to emit a top-level
    ``decision: block`` on stdout — the canonical Stop-block protocol.
    """
    path = Path(home) / "transcript.jsonl"
    entries = [
        {"type": "user", "message": {"role": "user", "content": [{"type": "text", "text": "go"}]}},
        {
            "type": "assistant",
            "message": {
                "role": "assistant",
                "content": [{"type": "text", "text": "Do you want me to merge this PR now?"}],
            },
        },
    ]
    path.write_text("\n".join(json.dumps(e) for e in entries), encoding="utf-8")
    return str(path)


def _run_router_with_transcript(event: str, transcript_path: str, home: str) -> subprocess.CompletedProcess[str]:
    env = {**os.environ, "HOME": home, "USERPROFILE": home}
    payload = {"session_id": "stop-block", "cwd": home, "transcript_path": transcript_path, "stop_hook_active": False}
    return subprocess.run(
        [sys.executable, str(HOOK_ROUTER), "--event", event],
        input=json.dumps(payload),
        capture_output=True,
        text=True,
        check=False,
        timeout=30,
        env=env,
    )


class TestStopBlockExitsZeroWithStdoutJson:
    """A Stop-event block must EXIT 0 with the decision JSON on stdout (#1764).

    For Stop/SubagentStop the harness contract inverts PreToolUse: exit 2 is a
    *blocking error* — the harness ignores stdout (and the ``decision: block``
    JSON in it) and feeds STDERR back to Claude. Exiting 2 on a Stop block
    therefore discards the reason and surfaces an empty "No stderr output"
    failure. The block must use exit 0 + ``{"decision":"block","reason":...}``
    on stdout so the reason reaches the agent.
    """

    def test_stop_structured_question_block_exits_0_not_2(self) -> None:
        with tempfile.TemporaryDirectory() as home:
            transcript = _stop_block_transcript(home)
            result = _run_router_with_transcript("Stop", transcript, home)

        assert result.returncode == 0, (
            f"a Stop block must exit 0 (got {result.returncode}); stderr was {result.stderr!r}"
        )
        out = json.loads(result.stdout)
        assert out["decision"] == "block"
        assert out["reason"], "the block reason must reach the agent via stdout"

    def test_stop_and_subagent_stop_are_json_decision_events(self) -> None:
        # SubagentStop shares the top-level-decision contract; a True-returning
        # handler there must likewise exit 0 with stdout JSON, never exit 2.
        from hooks.scripts import hook_router  # noqa: PLC0415

        assert {"Stop", "SubagentStop"} <= hook_router._JSON_DECISION_EVENTS
        assert "PreToolUse" not in hook_router._JSON_DECISION_EVENTS


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
