"""Tests for the force-slow-bash-to-background deny gate in hook_router (#1178).

A foreground Bash call that runs for minutes (a full test suite, a Playwright
run, a long sleep, a full container build) freezes the single-threaded loop
tick / orchestrator for its whole runtime. The gate DENIES the foreground call
with the exact one-flag fix (``run_in_background: true``) and a rare-case
``[fg-ok: <reason>]`` escape.

The gate is CONSERVATIVE: it denies ONLY high-confidence slow shapes and never
a fast command (a narrowed test run, a short sleep, git/grep), so a
no-false-deny guard accompanies the deny cases.
"""

import json

import pytest

from hooks.scripts.hook_router import handle_force_slow_bash_to_background


def _bash_event(command: str, tool_name: str = "Bash", *, run_in_background: bool | None = None) -> dict:
    tool_input: dict = {"command": command}
    if run_in_background is not None:
        tool_input["run_in_background"] = run_in_background
    return {
        "session_id": "sess-slow-bash",
        "tool_name": tool_name,
        "tool_input": tool_input,
    }


def _parse_deny(capsys: pytest.CaptureFixture[str]) -> dict | None:
    output = capsys.readouterr().out.strip()
    return json.loads(output) if output else None


class TestDeniesSlowForegroundCommands:
    """High-confidence slow foreground shapes are denied."""

    @pytest.mark.parametrize(
        "command",
        [
            # Unbounded full test suite — no narrowing.
            "uv run pytest",
            "pytest",
            "uv run pytest tests/",
            "uv run pytest --no-cov -q",
            "cd /repo && uv run pytest",
            # Browser E2E.
            "playwright test",
            "npx playwright test",
            "npx playwright test --project=chromium",
            "nx e2e my-app-e2e",
            "nx run my-app:e2e",
            # Full container build.
            "docker build -t img .",
            "docker compose build",
            # Long sleep.
            "sleep 60",
            "sleep 120",
            "sleep 90 && echo done",
        ],
    )
    def test_slow_foreground_command_is_denied(self, command: str, capsys: pytest.CaptureFixture[str]) -> None:
        assert handle_force_slow_bash_to_background(_bash_event(command)) is True
        deny = _parse_deny(capsys)
        assert deny is not None
        assert deny["permissionDecision"] == "deny"

    def test_deny_message_names_the_fix_and_escape(self, capsys: pytest.CaptureFixture[str]) -> None:
        handle_force_slow_bash_to_background(_bash_event("uv run pytest"))
        deny = _parse_deny(capsys)
        assert deny is not None
        reason = deny["permissionDecisionReason"]
        assert "run_in_background" in reason
        assert "loop" in reason
        assert "[fg-ok:" in reason
        assert "test suite" in reason


class TestAllowsBackgroundedAndOptedOut:
    """The two allow escapes pass through with no deny."""

    def test_run_in_background_true_is_allowed(self, capsys: pytest.CaptureFixture[str]) -> None:
        event = _bash_event("uv run pytest", run_in_background=True)
        assert handle_force_slow_bash_to_background(event) is not True
        assert capsys.readouterr().out.strip() == ""

    def test_run_in_background_false_still_gated(self, capsys: pytest.CaptureFixture[str]) -> None:
        event = _bash_event("uv run pytest", run_in_background=False)
        assert handle_force_slow_bash_to_background(event) is True

    def test_subagent_slow_bash_is_allowed(self, capsys: pytest.CaptureFixture[str]) -> None:
        # Sub-agents are the unrestricted hands: a foreground slow command in a
        # sub-agent ties up only its own thread, never the orchestrator loop.
        event = _bash_event("uv run pytest")
        event["agent_id"] = "a4ad83956ff699aaa"
        assert handle_force_slow_bash_to_background(event) is not True
        assert capsys.readouterr().out.strip() == ""

    @pytest.mark.parametrize(
        "command",
        [
            "uv run pytest [fg-ok: short-targeted-run]",
            "playwright test  [fg-ok: debugging a single spec]",
            "sleep 120 [fg-ok: intentional wait]",
        ],
    )
    def test_fg_ok_marker_is_allowed(self, command: str, capsys: pytest.CaptureFixture[str]) -> None:
        assert handle_force_slow_bash_to_background(_bash_event(command)) is not True
        assert capsys.readouterr().out.strip() == ""

    def test_empty_fg_ok_reason_does_not_unblock(self, capsys: pytest.CaptureFixture[str]) -> None:
        # An empty reason is not a valid opt-out — the slow command is still denied.
        assert handle_force_slow_bash_to_background(_bash_event("uv run pytest [fg-ok: ]")) is True


class TestAllowsFastCommands:
    """Fast / narrowed commands pass through — no false-deny."""

    @pytest.mark.parametrize(
        "command",
        [
            # Narrowed test runs.
            "uv run pytest -k foo",
            "uv run pytest -k 'test_thing or other'",
            "uv run pytest -x",
            "uv run pytest --lf",
            "uv run pytest --ff",
            "uv run pytest tests/test_hook_router_slow_bash_background.py",
            "uv run pytest tests/test_x.py::TestY::test_z",
            "pytest path/to/test_mod.py -q",
            # Short sleeps / variable sleep.
            "sleep 2",
            "sleep 5 && echo poll",
            "sleep $TIMEOUT",
            # Ordinary fast commands.
            "git status",
            "git commit -m 'wip'",
            "gh pr list",
            "glab mr view 7",
            "grep -rn pattern src/",
            "ls -la",
            "docker compose up -d",
            "docker ps",
            "nx build my-app",
            "echo 'run uv run pytest later'",
        ],
    )
    def test_command_is_allowed(self, command: str, capsys: pytest.CaptureFixture[str]) -> None:
        assert handle_force_slow_bash_to_background(_bash_event(command)) is not True
        assert capsys.readouterr().out.strip() == ""

    def test_ignores_non_bash_tools(self, capsys: pytest.CaptureFixture[str]) -> None:
        assert handle_force_slow_bash_to_background(_bash_event("uv run pytest", tool_name="Read")) is not True
        assert capsys.readouterr().out.strip() == ""

    def test_empty_command_passes_through(self, capsys: pytest.CaptureFixture[str]) -> None:
        assert handle_force_slow_bash_to_background(_bash_event("")) is not True
        assert capsys.readouterr().out.strip() == ""
