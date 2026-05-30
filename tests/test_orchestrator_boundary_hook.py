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


class TestPytestSubstringFalseDenyFixed:
    """``pytest`` is verb-anchored — a mention in an arg is NOT a false-deny.

    A bare word-boundary ``pytest`` match mis-denied the loop owner's
    ``git commit -m '…pytest…'`` / ``git branch x-pytest`` / ``uv add
    pytest-django`` (#1178 cold-review). The verb-position anchor only
    matches ``pytest`` as a command head (optionally after ``uv run`` /
    ``python -m`` or a shell separator), so these foreground main-agent
    commands now PASS.
    """

    @pytest.mark.parametrize(
        "command",
        [
            "git commit -m 'fix pytest fixture'",
            "git commit -m 'flaky pytest in CI'",
            "git branch 1178-feat-pytest-gate",
            "git checkout -b fix-pytest-flake",
            "git checkout -b fix-pytest",
            "uv add pytest-django",
            "uv add pytest-cov pytest-mock",
            "gh pr create --title 'add pytest gate'",
            "mkdir pytest-artifacts",
            "cat tests/test_pytest_helpers.py",
            "grep -rn pytest src/",
            "echo 'run pytest later'",
        ],
    )
    def test_pytest_mention_in_arg_passes(self, command: str) -> None:
        assert handle_enforce_orchestrator_boundary(_main_agent_bash(command)) is False

    @pytest.mark.parametrize(
        "command",
        [
            "pytest",
            "pytest -q",
            "uv run pytest",
            "python -m pytest",
            "python3 -m pytest tests/",
            "poetry run pytest",
            "uv run pytest --no-cov -q",
        ],
    )
    def test_real_pytest_invocation_still_denied(self, command: str) -> None:
        assert handle_enforce_orchestrator_boundary(_main_agent_bash(command)) is True


class TestMarginalSlowPatternsAdded:
    """The #1178-additive shapes the gate previously lacked are now denied.

    The gate already covered ``nx run …:e2e`` and ``docker compose
    build``, so the bare-target ``nx e2e`` and the image-build ``docker
    build`` are folded in. The interactive Django shells
    (``manage.py shell``/``shell_plus``/``dbshell``) are the original
    1h-hung RED-FLAG incident command and were not gated anywhere — added
    here. (``manage.py migrate`` is already redirected by the t3-CLI
    ``_BLOCKED_COMMANDS`` gate; short ``t3 loop tick``/``ci``/``doctor``
    are not slow and stay ungated.)
    """

    _MP = "manage.py "  # built at runtime; not a literal in source greps

    @pytest.mark.parametrize(
        "command",
        ["nx e2e my-app-e2e", "nx e2e frontend-e2e --watch", "docker build -t img .", "docker build ."],
    )
    def test_marginal_heavy_command_blocked_for_main_agent(self, command: str) -> None:
        assert handle_enforce_orchestrator_boundary(_main_agent_bash(command)) is True

    @pytest.mark.parametrize("subcommand", ["shell -c 'print(1)'", "shell_plus", "dbshell"])
    def test_interactive_django_shell_blocked_for_main_agent(self, subcommand: str) -> None:
        assert handle_enforce_orchestrator_boundary(_main_agent_bash("python " + self._MP + subcommand)) is True

    @pytest.mark.parametrize("command", ["nx e2e my-app-e2e", "docker build -t img ."])
    def test_marginal_heavy_command_exempt_for_subagent(self, command: str) -> None:
        assert handle_enforce_orchestrator_boundary(_subagent_bash(command)) is False

    def test_interactive_django_shell_exempt_for_subagent(self) -> None:
        assert handle_enforce_orchestrator_boundary(_subagent_bash("python " + self._MP + "shell -c 'x'")) is False

    @pytest.mark.parametrize("command", ["nx e2e my-app-e2e", "docker build -t img ."])
    def test_marginal_heavy_command_allowed_with_run_in_background(self, command: str) -> None:
        assert handle_enforce_orchestrator_boundary(_main_agent_bash(command, run_in_background=True)) is False

    def test_django_shellcheck_is_not_a_false_deny(self) -> None:
        # ``manage.py shellcheck`` (a hypothetical fast subcommand) must not
        # match the ``shell`` alternative — the ``\b`` anchor guards it.
        assert handle_enforce_orchestrator_boundary(_main_agent_bash(self._MP + "shellcheck")) is False


class TestForegroundOkEscapeHatch:
    """A ``[fg-ok: <reason>]`` marker opts a heavy command out of the gate.

    The per-call escape mirrors the ``[skip-plan-gate: <reason>]`` /
    ``[skip-skill-gate: <reason>]`` tokens — for the rare case the loop
    owner truly needs heavy output inline. A non-empty reason is required;
    an empty reason does not unblock.
    """

    @pytest.mark.parametrize(
        "command",
        [
            "uv run pytest [fg-ok: short-targeted-run]",
            "docker build -t img . [fg-ok: one-off image]",
            "nx e2e my-app-e2e  [fg-ok: debugging a single spec]",
            "sleep 600 [fg-ok: intentional wait]",
        ],
    )
    def test_fg_ok_marker_allows_heavy_command(self, command: str) -> None:
        assert handle_enforce_orchestrator_boundary(_main_agent_bash(command)) is False

    def test_empty_fg_ok_reason_does_not_unblock(self) -> None:
        assert handle_enforce_orchestrator_boundary(_main_agent_bash("uv run pytest [fg-ok: ]")) is True

    def test_block_message_mentions_fg_ok_escape(self, capsys: pytest.CaptureFixture[str]) -> None:
        assert handle_enforce_orchestrator_boundary(_main_agent_bash("uv run pytest")) is True
        out = json.loads(capsys.readouterr().out)
        assert "[fg-ok:" in out["permissionDecisionReason"]


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

    @pytest.mark.parametrize(
        "command",
        [
            "VAR=x t3 teatree gate disable",
            "t3 teatree gate disable > /tmp/out.log",
            "t3 teatree gate disable >| /tmp/out.log 2>&1",
        ],
    )
    def test_self_rescue_passes_even_with_env_prefix_or_redirect(self, command: str) -> None:
        # The escape hatch stays reachable when wrapped in the shell-grammar
        # shapes an agent naturally types (env-prefix, output redirect): none
        # of these turn the pure self-rescue into a heavy-denylist match.
        assert handle_enforce_orchestrator_boundary(_main_agent_bash(command)) is False

    def test_durable_killswitch_unlocks_every_command_for_the_main_agent(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # never-lockout: once the durable toml kill-switch is written (what
        # ``t3 <overlay> gate disable`` does), EVERY main-agent command —
        # including the heaviest foreground Bash — passes. The escape is
        # always effective, not merely reachable.
        (tmp_path / ".teatree.toml").write_text("[teatree]\norchestrator_bash_gate_enabled = false\n", encoding="utf-8")
        monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path))
        assert _orchestrator_bash_gate_enabled() is False
        assert handle_enforce_orchestrator_boundary(_main_agent_bash("uv run pytest")) is False
        assert handle_enforce_orchestrator_boundary(_main_agent_bash("docker compose up -d")) is False


class TestHeavyBashGateResistsShellGrammarBypass:
    """A heavy command can't be smuggled past the gate by shell-grammar tricks.

    The denylist matches the heavy token wherever it sits in the command line,
    so an env-prefix, a command separator (``;``/``&&``/``|``), or a trailing
    redirect cannot hide it. This is the dual of the self-rescue carve-out: the
    carve-out must stay narrow (only the pure ``t3 … gate`` form is exempt) so
    that pairing a self-rescue with a heavy command does not launder the heavy
    half through the exemption.
    """

    @pytest.mark.parametrize(
        "command",
        [
            "FOO=1 uv run pytest",
            "git status; uv run pytest",
            "git status && pytest -q",
            "echo hi | pytest",
            "uv run pytest > /tmp/out.log 2>&1",
            "t3 teatree gate disable && uv run pytest",
        ],
    )
    def test_heavy_command_is_blocked_despite_grammar_wrapping(self, command: str) -> None:
        assert handle_enforce_orchestrator_boundary(_main_agent_bash(command)) is True


class TestRegisteredInChain:
    def test_handler_is_in_pretooluse_chain(self) -> None:
        assert handle_enforce_orchestrator_boundary in router._HANDLERS["PreToolUse"]
