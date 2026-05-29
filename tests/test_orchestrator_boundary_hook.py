"""Tests for the orchestrator-execution-boundary gate (#836 §17.6 gate 2, #115).

The orchestrator (MAIN agent) keeps its session responsive: it
dispatches sub-agents and decides merges/clears, and should not tie its
own session up running a LONG / HEAVY foreground Bash command (test
suite, build, dev server, long sleep, full-tree sweep). Quick
orientation Bash — ``git status``/``cat``/``ls``/``grep``/``git
commit`` — passes; only the heavy denylist shapes are gated, and
``run_in_background: true`` is the escape hatch. Sub-agents — the hands
that implement — may run anything.

#115 fixed the two original defects: (a) the gate was an allow-list that
over-blocked quick orchestrator Bash, now a denylist; (b) it
MISDETECTED genuine sub-agents as the main agent because the PreToolUse
payload's ``transcript_path`` always points at the PARENT session
transcript (``isSidechain: false`` tail), never the sub-agent's own. The
reliable signal is the payload's ``agent_id`` (non-empty ⇒ sub-agent),
read by ``_call_is_from_subagent``.
"""

import json
from pathlib import Path

import pytest

import hooks.scripts.hook_router as router
from hooks.scripts.hook_router import (
    _call_is_from_subagent,
    _is_orchestration_action,
    _orchestrator_bash_gate_enabled,
    handle_enforce_orchestrator_boundary,
)


@pytest.fixture(autouse=True)
def _gate_enabled_home(tmp_path_factory: pytest.TempPathFactory, monkeypatch: pytest.MonkeyPatch) -> None:
    """Isolate ``_orchestrator_bash_gate_enabled`` from the dev's real config.

    The handler reads ``~/.teatree.toml``; the developer's real file may
    set ``orchestrator_bash_gate_enabled = false`` (the #115 failsafe).
    Point ``Path.home`` at a clean tmp dir so the gate is ON by default
    for every test here. The kill-switch tests monkeypatch ``Path.home``
    again to their own dir, overriding this fixture.
    """
    home = tmp_path_factory.mktemp("home")
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: home))


def _main_agent_bash(command: str, *, run_in_background: bool | None = None) -> dict:
    """A main-agent Bash payload (no ``agent_id``)."""
    tool_input: dict = {"command": command}
    if run_in_background is not None:
        tool_input["run_in_background"] = run_in_background
    return {"tool_name": "Bash", "tool_input": tool_input}


def _subagent_bash(command: str) -> dict:
    """A sub-agent Bash payload — carries a non-empty ``agent_id``."""
    return {
        "tool_name": "Bash",
        "tool_input": {"command": command},
        "agent_id": "a4ad83956ff699aaa",
        "agent_type": "general-purpose",
    }


class TestCallIsFromSubagent:
    def test_nonempty_agent_id_is_subagent(self) -> None:
        assert _call_is_from_subagent({"agent_id": "a4ad83956ff699aaa"}) is True

    def test_absent_agent_id_is_main_agent(self) -> None:
        assert _call_is_from_subagent({"tool_name": "Bash"}) is False

    def test_empty_agent_id_is_main_agent(self) -> None:
        assert _call_is_from_subagent({"agent_id": ""}) is False


class TestOrchestrationAction:
    def test_task_dispatch_is_orchestration(self) -> None:
        assert _is_orchestration_action({"tool_name": "Task", "tool_input": {}}) is True

    def test_ask_user_question_is_orchestration(self) -> None:
        assert _is_orchestration_action({"tool_name": "AskUserQuestion", "tool_input": {}}) is True

    def test_mcp_send_message_is_orchestration(self) -> None:
        assert _is_orchestration_action({"tool_name": "mcp__claude_ai_Slack__slack_send_message"}) is True

    def test_mcp_view_read_is_orchestration(self) -> None:
        assert _is_orchestration_action({"tool_name": "mcp__claude_ai_Slack__slack_read"}) is True

    def test_bash_is_not_decided_here(self) -> None:
        # Bash is judged by the heavy denylist in the handler, not here.
        assert _is_orchestration_action(_main_agent_bash("git status")) is False


class TestMainAgentQuickBashAllowed:
    """Quick orientation/mutation Bash from the main agent passes through.

    These were BLOCKED under the old allow-list (#115 over-block); the
    denylist inversion lets them through.
    """

    @pytest.mark.parametrize(
        "command",
        [
            "git status",
            "git commit -m 'wip'",
            "cat src/teatree/config.py",
            "grep -rn TODO src/",
            "ls -la",
            "echo hello",
            "rg pattern src/",
            "head -50 file.py",
            "sed -i 's/a/b/' file.py",
            "gh pr view 42 --json state | grep state",
            "t3 teatree ticket merge 7 --human-authorized owner",
        ],
    )
    def test_quick_main_agent_bash_passes(self, command: str) -> None:
        assert handle_enforce_orchestrator_boundary(_main_agent_bash(command)) is False


class TestMainAgentHeavyBashBlocked:
    """Heavy/long-running foreground Bash from the main agent is denied."""

    @pytest.mark.parametrize(
        "command",
        [
            "uv run pytest --no-cov -q",
            "tox -e py312",
            "t3 teatree run backend",
            "t3 myapp e2e smoke",
            "python manage.py runserver",
            "nx serve frontend",
            "docker compose up -d",
            "npx playwright test",
            "playwright test specs/",
            "npm run build",
            "npm install",
            "npm ci",
            "pipenv install",
            "pip install requests",
            "uv sync",
            "vite build",
            "webpack --mode production",
            "cargo build --release",
            "cargo test",
            "make all",
            "sleep 600",
            "find . -name '*.py' -exec grep -l TODO {} ;",
            "ls -laR /Users/adrien/workspace",
        ],
    )
    def test_heavy_main_agent_bash_blocked(self, command: str) -> None:
        assert handle_enforce_orchestrator_boundary(_main_agent_bash(command)) is True

    def test_block_message_mentions_run_in_background_and_kill_switch(self, capsys: pytest.CaptureFixture[str]) -> None:
        assert handle_enforce_orchestrator_boundary(_main_agent_bash("uv run pytest")) is True
        out = json.loads(capsys.readouterr().out)
        assert out["permissionDecision"] == "deny"
        reason = out["permissionDecisionReason"]
        assert "long-running" in reason or "heavy" in reason
        assert "run_in_background" in reason
        assert "orchestrator_bash_gate_enabled" in reason


class TestHeavyBashEscapeHatch:
    def test_heavy_with_run_in_background_is_allowed(self) -> None:
        assert handle_enforce_orchestrator_boundary(_main_agent_bash("uv run pytest", run_in_background=True)) is False

    def test_subagent_heavy_bash_is_allowed(self) -> None:
        # The #115 regression test: a genuine sub-agent (non-empty
        # ``agent_id``) running a heavy command must NOT be blocked, even
        # though the payload's transcript_path would read isSidechain:false.
        assert handle_enforce_orchestrator_boundary(_subagent_bash("uv run pytest --no-cov -q")) is False

    def test_subagent_dev_server_is_allowed(self) -> None:
        assert handle_enforce_orchestrator_boundary(_subagent_bash("nx serve frontend")) is False


class TestNonBashToolsArePassThrough:
    """The gate now only governs Bash — Edit/Write/Read/Grep pass through.

    Investigative/implementation tools are no longer blocked for the main
    agent (4.x-class agents inspect freely); the boundary is narrowed to
    heavy Bash only.
    """

    @pytest.mark.parametrize("tool_name", ["Edit", "Write", "NotebookEdit", "Read", "Grep", "Glob"])
    def test_non_bash_tool_passes(self, tool_name: str) -> None:
        assert handle_enforce_orchestrator_boundary({"tool_name": tool_name, "tool_input": {}}) is False


class TestGateKillSwitch:
    def test_gate_disabled_via_toml_passes_all(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        home = tmp_path
        (home / ".teatree.toml").write_text("[teatree]\norchestrator_bash_gate_enabled = false\n", encoding="utf-8")
        monkeypatch.setattr(Path, "home", classmethod(lambda cls: home))
        assert _orchestrator_bash_gate_enabled() is False
        # Even a heavy foreground main-agent command passes when disabled.
        assert handle_enforce_orchestrator_boundary(_main_agent_bash("uv run pytest")) is False

    def test_gate_enabled_by_default_when_key_absent(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        home = tmp_path
        (home / ".teatree.toml").write_text("[teatree]\n", encoding="utf-8")
        monkeypatch.setattr(Path, "home", classmethod(lambda cls: home))
        assert _orchestrator_bash_gate_enabled() is True

    def test_gate_enabled_when_config_missing(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path))
        assert _orchestrator_bash_gate_enabled() is True

    def test_gate_enabled_on_broken_toml(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        (tmp_path / ".teatree.toml").write_text("this is not = valid = toml [[[", encoding="utf-8")
        monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path))
        assert _orchestrator_bash_gate_enabled() is True


class TestMainAgentForegroundAgentIsBlocked1442:
    """#1442 — main-agent Agent dispatch must pass ``run_in_background``.

    Detection now uses ``agent_id`` (the #115 fix) instead of the
    transcript ``isSidechain`` read.
    """

    _RULE_CITATION = "feedback_always_run_in_background_for_sub_agent_dispatch"

    def test_agent_foreground_blocked_in_main_agent(self, capsys: pytest.CaptureFixture[str]) -> None:
        data = {"tool_name": "Agent", "tool_input": {"description": "implement X", "run_in_background": False}}
        assert handle_enforce_orchestrator_boundary(data) is True
        out = json.loads(capsys.readouterr().out)
        assert out["permissionDecision"] == "deny"
        assert "main-agent-orchestration-guard" in out["permissionDecisionReason"]
        assert "run_in_background" in out["permissionDecisionReason"]
        assert self._RULE_CITATION in out["permissionDecisionReason"]

    def test_agent_foreground_blocked_when_field_absent(self, capsys: pytest.CaptureFixture[str]) -> None:
        data = {"tool_name": "Agent", "tool_input": {"description": "implement X"}}
        assert handle_enforce_orchestrator_boundary(data) is True
        out = json.loads(capsys.readouterr().out)
        assert out["permissionDecision"] == "deny"
        assert self._RULE_CITATION in out["permissionDecisionReason"]

    def test_agent_background_allowed_in_main_agent(self) -> None:
        data = {"tool_name": "Agent", "tool_input": {"description": "implement X", "run_in_background": True}}
        assert handle_enforce_orchestrator_boundary(data) is False

    def test_agent_foreground_allowed_in_sub_agent(self) -> None:
        # Sub-agent (non-empty agent_id) dispatching its own Agent may
        # pick foreground — the guard only governs main-agent dispatch.
        data = {
            "tool_name": "Agent",
            "tool_input": {"description": "nested work", "run_in_background": False},
            "agent_id": "a4ad83956ff699aaa",
            "agent_type": "general-purpose",
        }
        assert handle_enforce_orchestrator_boundary(data) is False


class TestSelfRescueEscapeHatchNeverGated:
    r"""The self-rescue command can NEVER be gated (#1474).

    ``t3 <overlay> gate disable`` is the orchestrator's guaranteed escape
    from a Bash lockout: it flips the durable ``orchestrator_bash_gate_enabled``
    kill-switch in ``~/.teatree.toml``. For the escape to be reachable EVEN
    WHEN the gate is fully enabled — and even if sidechain detection
    misclassifies the caller — the heavy-Bash denylist must not match it.

    These tests pin that invariant. They go RED the moment anyone adds
    ``t3 … gate`` to :data:`_ORCHESTRATOR_HEAVY_BASH_RE` (e.g. by widening the
    ``t3 \S+ (run|e2e|test)`` alternative to also catch ``gate``).
    """

    @pytest.mark.parametrize("command", ["t3 teatree gate disable", "t3 teatree gate enable", "t3 teatree gate status"])
    def test_self_rescue_not_matched_by_heavy_denylist(self, command: str) -> None:
        assert router._ORCHESTRATOR_HEAVY_BASH_RE.search(command) is None

    @pytest.mark.parametrize(
        "command",
        ["t3 teatree gate disable", "t3-teatree gate disable", "t3 myoverlay gate disable"],
    )
    def test_main_agent_self_rescue_passes_with_gate_enabled(self, command: str) -> None:
        # MAIN-agent call (no agent_id), gate fully ON (the autouse fixture
        # points Path.home at a clean tmp dir): the escape hatch must pass.
        assert _orchestrator_bash_gate_enabled() is True
        assert handle_enforce_orchestrator_boundary(_main_agent_bash(command)) is False


class TestRegisteredInChain:
    def test_handler_is_in_pretooluse_chain(self) -> None:
        assert handle_enforce_orchestrator_boundary in router._HANDLERS["PreToolUse"]
