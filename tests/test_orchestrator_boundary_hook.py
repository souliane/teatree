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
    _orchestrator_boundary_agent_gate_enabled,
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


class TestHelpAndVersionQueriesAllowed:
    """A --help/--version query of a heavy verb is fast/read-only — not gated.

    Anti-vacuous: the help/version FALSE POSITIVES now pass, while a genuinely
    heavy invocation of the SAME verb (and a help-arm chained to a heavy arm)
    still denies — proving the exemption did not weaken the gate.
    """

    @pytest.mark.parametrize(
        "command",
        [
            "t3 dream run --help",
            "t3 teatree run backend --help",
            "docker build --help",
            "docker compose up --help",
            "pytest -h",
            "npm run --help",
            "make --version",
        ],
    )
    def test_help_or_version_query_is_allowed(self, command: str) -> None:
        assert handle_enforce_orchestrator_boundary(_main_agent_bash(command)) is False

    @pytest.mark.parametrize(
        "command",
        [
            "t3 dream run",  # same verb, no --help → still heavy
            "docker build .",
            "t3 x run --help && pytest tests/",  # help arm + a genuinely heavy arm
            "ls -lhR /",  # has -h but as a flag bundle, not a help token
            # A -h after `find … -exec` is the EXEC'd command's flag, never a
            # find help query — the recursive sweep must stay denied.
            r"find /src -name '*.py' -exec grep -h 'TODO' {} \;",  # grep -h = suppress filename
            "find . -type f -exec chmod -h {} +",  # chmod -h = no-dereference
            r"find / -name '*.log' -exec rm -h {} \;",
            "find . -exec du -h {} +",  # du -h = human-readable
            "git push --help",  # opens a blocking pager — a worse wedge
            "git push -h",
        ],
    )
    def test_genuinely_heavy_still_denied(self, command: str) -> None:
        assert handle_enforce_orchestrator_boundary(_main_agent_bash(command)) is True


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


class TestPytestUvxAndWrapperPrefixesAnchored:
    r"""``uvx pytest`` and wrapper-prefixed ``pytest`` are now anchored (#1576).

    The runner-prefix group previously required ``run`` after EVERY runner,
    so ``uvx pytest`` (uvx takes no ``run``) slipped to the foreground; and
    common command wrappers (``command``/``exec``/``time``/``nice``) were
    absent, so ``command pytest`` etc. also slipped. Both are folded into
    the verb anchor in the safe deny-more direction. The trailing
    ``pytest(?![\\w-])`` keeps the match pinned to ``pytest`` — wrapper
    prefixes never widen to other tools.
    """

    @pytest.mark.parametrize(
        "command",
        [
            "uvx pytest",
            "uvx pytest tests/",
            "command pytest",
            "exec pytest",
            "time pytest",
            "nice pytest",
            "command exec time nice pytest",
            "time uvx pytest",
            "pdm run pytest",
            "hatch run pytest",
        ],
    )
    def test_uvx_and_wrapper_prefixed_pytest_now_denied(self, command: str) -> None:
        assert handle_enforce_orchestrator_boundary(_main_agent_bash(command)) is True

    @pytest.mark.parametrize(
        "command",
        [
            "uvx ruff",
            "uvx ruff check",
            "command ls",
            "exec ls -la",
            "time git status",
            "nice cat README.md",
        ],
    )
    def test_wrapper_prefixes_do_not_widen_beyond_pytest(self, command: str) -> None:
        assert handle_enforce_orchestrator_boundary(_main_agent_bash(command)) is False

    def test_uvx_pytest_fg_ok_escape_still_works(self) -> None:
        assert handle_enforce_orchestrator_boundary(_main_agent_bash("uvx pytest [fg-ok: targeted run]")) is False

    def test_uvx_pytest_run_in_background_still_allowed(self) -> None:
        assert handle_enforce_orchestrator_boundary(_main_agent_bash("uvx pytest", run_in_background=True)) is False

    @pytest.mark.parametrize("command", ["pytest_helper.py", "pytest-django setup", "x-pytest"])
    def test_pytest_followed_by_word_or_hyphen_not_denied(self, command: str) -> None:
        assert handle_enforce_orchestrator_boundary(_main_agent_bash(command)) is False


class TestHandlerCrashProofOnMissingOrNonStrCommand:
    """The handler self-guards a missing / ``None`` / non-``str`` command (#1576).

    The router's per-handler try/except already makes a ``TypeError`` net
    fail-open, but the handler's docstring implies it is self-crash-proof.
    The in-function ``isinstance`` guard returns the clean allow value
    (no match) WITHOUT relying on the outer catch — no ``TypeError``.
    """

    @pytest.mark.parametrize(
        "tool_input",
        [
            {"command": None},
            {},
            {"command": 123},
            {"command": ["pytest"]},
        ],
    )
    def test_non_str_or_missing_command_fails_open(self, tool_input: dict) -> None:
        data = {"tool_name": "Bash", "tool_input": tool_input}
        assert handle_enforce_orchestrator_boundary(data) is False


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

    The per-call escape mirrors the ``[skip-skill-gate: <reason>]`` token —
    for the rare case the loop
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

    Detection uses ``agent_id`` (the #115 fix) instead of the transcript
    ``isSidechain`` read. Since #1733 the Agent-arm deny is default-ON (an
    ``Agent`` PreToolUse matcher is wired in hooks.json per #1646, and the
    deny routes through ``_fail_open_or_deny`` per #1692). This fixture pins
    the flag ON explicitly to exercise the deny logic; the kill-switch / escape
    paths below prove the no-lockout off-ramps.
    """

    _RULE_CITATION = "feedback_always_run_in_background_for_sub_agent_dispatch"

    @pytest.fixture(autouse=True)
    def _agent_gate_on(self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
        home = tmp_path / "home"
        home.mkdir(parents=True, exist_ok=True)
        (home / ".teatree.toml").write_text(
            "[teatree]\norchestrator_boundary_agent_gate_enabled = true\n", encoding="utf-8"
        )
        monkeypatch.setattr(Path, "home", classmethod(lambda _cls: home))

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

    def test_agent_foreground_allowed_when_kill_switch_set(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        # The gate is now default-ON (#1733); the OFF path is the explicit
        # kill-switch. With it set, a foreground dispatch passes (no lockout).
        kill_home = tmp_path / "kill-home"
        kill_home.mkdir(parents=True, exist_ok=True)
        (kill_home / ".teatree.toml").write_text(
            "[teatree]\norchestrator_boundary_agent_gate_enabled = false\n", encoding="utf-8"
        )
        monkeypatch.setattr(Path, "home", classmethod(lambda _cls: kill_home))
        data = {"tool_name": "Agent", "tool_input": {"description": "implement X", "run_in_background": False}}
        assert handle_enforce_orchestrator_boundary(data) is False
        assert capsys.readouterr().out.strip() == ""

    def test_agent_background_allowed_in_main_agent(self) -> None:
        data = {"tool_name": "Agent", "tool_input": {"description": "implement X", "run_in_background": True}}
        assert handle_enforce_orchestrator_boundary(data) is False

    def test_agent_foreground_fg_ok_token_allowed(self) -> None:
        data = {
            "tool_name": "Agent",
            "tool_input": {"prompt": "[fg-ok: attended] implement X", "run_in_background": False},
        }
        assert handle_enforce_orchestrator_boundary(data) is False

    def test_agent_foreground_empty_fg_ok_reason_still_denied(self, capsys: pytest.CaptureFixture[str]) -> None:
        data = {"tool_name": "Agent", "tool_input": {"prompt": "[fg-ok: ] implement X", "run_in_background": False}}
        assert handle_enforce_orchestrator_boundary(data) is True
        assert json.loads(capsys.readouterr().out)["permissionDecision"] == "deny"

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


class TestAgentGateDefaultOn1733:
    """#1733 / #1646 — the foreground-Agent gate flips to DEFAULT-ON.

    The Agent arm of the orchestrator-boundary gate is now wired (an ``Agent``
    PreToolUse matcher exists in hooks.json) and default-enabled. A bare
    foreground main-agent Agent dispatch with NO config at all is DENIED
    (proving the flip is live), while every off-ramp + always-allowed tool
    stays allowed even with no kill-switch written. The attended dry-run that
    #1733 asks for is the user's pre-INSTALL gate, not a blocker to the code:
    these tests pin the default-ON behaviour and the never-lockout off-ramps.
    """

    @pytest.fixture(autouse=True)
    def _empty_home(self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
        # No ~/.teatree.toml at all — the gate must be ON by its new default.
        empty_home = tmp_path / "empty-home"
        empty_home.mkdir(parents=True, exist_ok=True)
        monkeypatch.setattr(Path, "home", classmethod(lambda _cls: empty_home))
        monkeypatch.setenv("HOME", str(empty_home))

    def test_gate_enabled_by_default_when_config_absent(self) -> None:
        assert _orchestrator_boundary_agent_gate_enabled() is True

    def test_bare_foreground_agent_denied_by_default(self, capsys: pytest.CaptureFixture[str]) -> None:
        data = {"tool_name": "Agent", "tool_input": {"description": "implement X", "run_in_background": False}}
        assert handle_enforce_orchestrator_boundary(data) is True
        out = json.loads(capsys.readouterr().out)
        assert out["permissionDecision"] == "deny"
        assert "main-agent-orchestration-guard" in out["permissionDecisionReason"]

    def test_background_agent_allowed_by_default(self) -> None:
        data = {"tool_name": "Agent", "tool_input": {"description": "implement X", "run_in_background": True}}
        assert handle_enforce_orchestrator_boundary(data) is False

    def test_fg_ok_token_allowed_by_default(self) -> None:
        data = {"tool_name": "Agent", "tool_input": {"prompt": "[fg-ok: attended] go", "run_in_background": False}}
        assert handle_enforce_orchestrator_boundary(data) is False

    def test_subagent_dispatch_allowed_by_default(self) -> None:
        data = {
            "tool_name": "Agent",
            "tool_input": {"description": "nested", "run_in_background": False},
            "agent_id": "a4ad83956ff699aaa",
        }
        assert handle_enforce_orchestrator_boundary(data) is False

    def test_explicit_kill_switch_disables_default_on_gate(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        home = tmp_path / "kill"
        home.mkdir(parents=True, exist_ok=True)
        (home / ".teatree.toml").write_text(
            "[teatree]\norchestrator_boundary_agent_gate_enabled = false\n", encoding="utf-8"
        )
        monkeypatch.setattr(Path, "home", classmethod(lambda _cls: home))
        assert _orchestrator_boundary_agent_gate_enabled() is False
        data = {"tool_name": "Agent", "tool_input": {"description": "implement X", "run_in_background": False}}
        assert handle_enforce_orchestrator_boundary(data) is False


class TestAgentDenyRoutesThroughFailOpen1692:
    """#1692 — the foreground-Agent deny routes through ``_fail_open_or_deny``.

    Before #1692 the Agent arm called ``emit_pretooluse_deny`` directly, so the
    master ``[teatree] danger_gate_fail_open`` kill-switch and the self-rescue
    allowlist (the never-lockout machinery every other over-deny gate funnels
    through) did NOT apply to it. After #1692 a foreground Agent dispatch is
    relaxed by the master fail-open switch exactly like every other over-deny
    gate — even with the gate itself ON.
    """

    @pytest.fixture
    def home(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
        home = tmp_path / "home"
        home.mkdir(parents=True, exist_ok=True)
        monkeypatch.setattr(Path, "home", classmethod(lambda _cls: home))
        monkeypatch.setenv("HOME", str(home))
        return home

    def _enable_gate(self, home: Path, *, master_fail_open: bool) -> None:
        lines = ["[teatree]", "orchestrator_boundary_agent_gate_enabled = true"]
        if master_fail_open:
            lines.append("danger_gate_fail_open = true")
        (home / ".teatree.toml").write_text("\n".join(lines) + "\n", encoding="utf-8")

    def _fg_agent(self) -> dict:
        return {"tool_name": "Agent", "tool_input": {"description": "implement X", "run_in_background": False}}

    def test_master_fail_open_relaxes_foreground_agent_deny(
        self, home: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        # Gate ON + master fail-open ON → the foreground deny is relaxed,
        # which is only possible if the deny routed through _fail_open_or_deny.
        self._enable_gate(home, master_fail_open=True)
        assert handle_enforce_orchestrator_boundary(self._fg_agent()) is False
        assert capsys.readouterr().out.strip() == ""

    def test_foreground_agent_still_denied_when_master_fail_open_off(
        self, home: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        # Anchor: gate ON, master fail-open OFF → the deny still fires (the
        # fail-open routing is an escape, not a defanged gate).
        self._enable_gate(home, master_fail_open=False)
        assert handle_enforce_orchestrator_boundary(self._fg_agent()) is True
        assert capsys.readouterr().out.strip()


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
