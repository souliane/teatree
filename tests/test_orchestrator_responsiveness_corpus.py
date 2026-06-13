"""Golden corpus for orchestrator responsiveness — symmetric never-lockout guard.

The orchestrator (MAIN agent) stays responsive only if it (a) never ties its
session up in a long foreground OPERATION and (b) never grinds through a long
TURN of many tool calls before yielding to the user. Two gates cover those:
:func:`handle_enforce_orchestrator_boundary` (heavy-Bash / foreground-Agent
deny) and :func:`handle_orchestrator_turn_budget_nudge` (soft per-turn tool-call
nudge).

This corpus pins BOTH failure dimensions through the REAL gate functions.

MUST-ALLOW — the orchestration vocabulary the gates must NEVER block: talking
to the user, dispatching sub-agents, the task ledger, skill loads, quick
reads/greps/globs, and ``t3 ... status``/``show``/``loop status``. A
regression here is a LOCKOUT (the orchestrator can no longer orchestrate).

MUST-DENY / WARN — the foreground heavy work the orchestrator must hand off:
inline test suites, ``t3 update``, ``git reset``/``rebase``, CI waits,
long-running ops, git-archaeology loops, and foreground sub-agent dispatch. A
regression here is a BYPASS (foreground grind slips through).

A gate that blocks orchestration is as broken as one that lets foreground
coding through; this file fails on either.
"""

import json
from pathlib import Path

import pytest

import hooks.scripts.hook_router as router
from hooks.scripts.hook_router import (
    _ORCHESTRATOR_HEAVY_BASH_RE,
    handle_enforce_orchestrator_boundary,
    handle_orchestrator_turn_budget_nudge,
    handle_reset_turn_tool_budget,
)


@pytest.fixture(autouse=True)
def clean_home(tmp_path_factory: pytest.TempPathFactory, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Pin ``Path.home`` at a clean tmp dir so every gate runs at its default.

    The dev's real ``~/.teatree.toml`` may disable the bash gate (the #115
    failsafe) or set a non-default turn budget; isolate from it so the corpus
    exercises the protective defaults.
    """
    home = tmp_path_factory.mktemp("home")
    monkeypatch.setattr(Path, "home", classmethod(lambda _cls: home))
    return home


@pytest.fixture(autouse=True)
def _isolated_state(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Point the per-session state dir at a fresh tmp dir per test."""
    state = tmp_path / "state"
    state.mkdir()
    monkeypatch.setattr(router, "STATE_DIR", state)
    return state


def _main_bash(command: str, *, run_in_background: bool | None = None) -> dict:
    tool_input: dict = {"command": command}
    if run_in_background is not None:
        tool_input["run_in_background"] = run_in_background
    return {"tool_name": "Bash", "tool_input": tool_input, "session_id": "s-corpus"}


def _subagent_bash(command: str) -> dict:
    return {
        "tool_name": "Bash",
        "tool_input": {"command": command},
        "session_id": "s-corpus",
        "agent_id": "a4ad83956ff699aaa",
    }


def _main_tool(tool_name: str, **tool_input: object) -> dict:
    return {"tool_name": tool_name, "tool_input": tool_input, "session_id": "s-corpus"}


# ── MUST-ALLOW: orchestration vocabulary is never blocked ───────────────────────

# Pure-orchestration tools — dispatch, the task ledger, asking/talking to the
# user, skill loads. The boundary gate must pass them through untouched.
#
# NOTE: a BARE foreground ``Agent`` is intentionally NOT in this list — with the
# #1733 default-ON Agent gate, a foreground Agent dispatch is denied unless it
# carries an off-ramp (``run_in_background: true`` / ``[fg-ok:]`` / sub-agent
# context). The background-Agent allow + the foreground-Agent deny are pinned
# explicitly in ``TestForegroundAgentBoundary`` below.
_MUST_ALLOW_ORCHESTRATION_TOOLS = [
    "AskUserQuestion",
    "SendMessage",
    "Task",
    "TaskCreate",
    "TaskUpdate",
    "TaskList",
    "TaskGet",
    "Skill",
    "Read",
    "Grep",
    "Glob",
    "Edit",
    "Write",
    "NotebookEdit",
]

# Quick orientation / status Bash the orchestrator routinely needs.
# Read-only git, a targeted/single test run, and the orchestration
# vocabulary all stay ALLOWED — only a foreground heavy OPERATION
# (whole-suite test, ``git push``) is gated (#1825).
_MUST_ALLOW_BASH = [
    "git status",
    "git log --oneline -5",
    "git diff --stat",
    "git diff",
    "git show HEAD",
    "git fetch origin",
    "cat src/teatree/config.py",
    "grep -rn TODO src/",
    "rg pattern src/",
    "ls -la",
    "gh pr view 42 --json state",
    "glab mr list",
    "t3 teatree worktree status",
    "t3 loop status",
    "t3 teatree followup sync",
    "t3 teatree gate disable",
    # #1825 — a single / targeted pytest run is cheap; never gate it.
    "pytest -k test_foo",
    "pytest tests/x.py::Test::test_z",
    "pytest path/to/test_file.py",
    "uv run pytest -k test_foo",
    'pytest -k "foo or bar"',
    "uv run pytest --no-cov -q tests/test_orchestrator_responsiveness_corpus.py",
]


class TestMustAllowNeverBlocked:
    @pytest.mark.parametrize("tool_name", _MUST_ALLOW_ORCHESTRATION_TOOLS)
    def test_orchestration_tool_passes_boundary_gate(self, tool_name: str) -> None:
        assert handle_enforce_orchestrator_boundary(_main_tool(tool_name)) is False

    @pytest.mark.parametrize("command", _MUST_ALLOW_BASH)
    def test_quick_bash_passes_boundary_gate(self, command: str) -> None:
        assert handle_enforce_orchestrator_boundary(_main_bash(command)) is False

    @pytest.mark.parametrize("command", _MUST_ALLOW_BASH)
    def test_quick_bash_never_emits_deny(self, command: str, capsys: pytest.CaptureFixture[str]) -> None:
        handle_enforce_orchestrator_boundary(_main_bash(command))
        assert capsys.readouterr().out.strip() == ""


# ── MUST-DENY: foreground heavy work the orchestrator must hand off ──────────────

_MUST_DENY_FOREGROUND_BASH = [
    "uv run pytest",
    "uv run pytest --no-cov -q",
    # #1825 — bare / whole-suite / directory pytest is the gated shape.
    "pytest",
    "pytest tests/",
    "tox -e py312",
    "t3 teatree run backend",
    "t3 myapp e2e smoke",
    "npx playwright test",
    "npm run build",
    "npm install",
    "uv sync",
    "docker compose up -d",
    "make all",
    "sleep 600",
    "find . -name '*.py' -exec grep -l TODO {} ;",
    "ls -laR /Users/adrien/workspace",
    # #1825 — a foreground ``git push`` runs the full pre-push suite and
    # wedges the loop owner's session (the motivating incident).
    "git push",
    "git push origin HEAD",
    "git push -u origin feature",
    "git push --force-with-lease origin feature",
]


class TestMustDenyForegroundHeavyWork:
    @pytest.mark.parametrize("command", _MUST_DENY_FOREGROUND_BASH)
    def test_foreground_heavy_bash_denied(self, command: str) -> None:
        assert handle_enforce_orchestrator_boundary(_main_bash(command)) is True

    @pytest.mark.parametrize("command", _MUST_DENY_FOREGROUND_BASH)
    def test_same_heavy_bash_allowed_in_background(self, command: str) -> None:
        assert handle_enforce_orchestrator_boundary(_main_bash(command, run_in_background=True)) is False


# #1825 — git push is the gate's motivating incident (its pre-push suite
# wedges the session); read-only git must never be gated.
_GIT_PUSH_DENY = [
    "git push",
    "git push origin HEAD",
    "git push -u origin feature",
    "git push --force-with-lease origin feature",
    "git push --force",
]
_READONLY_GIT_ALLOW = [
    "git status",
    "git log --oneline -5",
    "git diff",
    "git show HEAD",
    "git fetch origin",
    "git commit -m 'push the button'",
    "git branch push-fix",
    "git checkout -b fix-push",
]
# #1825 — a single/targeted pytest run is allowed; only the whole suite
# (and a directory arg) is gated.
_TARGETED_PYTEST_ALLOW = [
    "pytest -k test_foo",
    "pytest tests/x.py::Test::test_z",
    "pytest path/to/test_file.py",
    "uv run pytest -k test_foo",
    'pytest -k "foo or bar"',
    "python -m pytest tests/test_x.py",
    "pytest tests/test_x.py::test_y",
    "uvx pytest tests/test_x.py",
]
_WHOLE_SUITE_PYTEST_DENY = [
    "pytest",
    "pytest -q",
    "pytest tests/",
    "uv run pytest",
    "uv run pytest --no-cov -q",
    "python -m pytest tests/",
    "poetry run pytest",
    "uvx pytest",
    "uvx pytest tests/",
]


class TestGitPushBoundary:
    """#1825 — a foreground ``git push`` is gated; read-only git is not.

    ``git push`` is the gate's motivating incident (its full pre-push
    suite blocks the loop and the user's queued input). It must deny in
    the foreground main agent while honouring every never-lockout
    off-ramp, and read-only git must never be gated.
    """

    @pytest.mark.parametrize("command", _GIT_PUSH_DENY)
    def test_foreground_git_push_denied(self, command: str) -> None:
        assert handle_enforce_orchestrator_boundary(_main_bash(command)) is True

    @pytest.mark.parametrize("command", _GIT_PUSH_DENY)
    def test_git_push_allowed_in_background(self, command: str) -> None:
        assert handle_enforce_orchestrator_boundary(_main_bash(command, run_in_background=True)) is False

    @pytest.mark.parametrize("command", _GIT_PUSH_DENY)
    def test_git_push_allowed_from_subagent(self, command: str) -> None:
        assert handle_enforce_orchestrator_boundary(_subagent_bash(command)) is False

    def test_git_push_allowed_with_fg_ok_marker(self) -> None:
        assert handle_enforce_orchestrator_boundary(_main_bash("git push [fg-ok: release cut]")) is False

    def test_git_push_allowed_when_kill_switch_set(self, clean_home: Path) -> None:
        (clean_home / ".teatree.toml").write_text(
            "[teatree]\norchestrator_bash_gate_enabled = false\n", encoding="utf-8"
        )
        assert handle_enforce_orchestrator_boundary(_main_bash("git push")) is False

    @pytest.mark.parametrize("command", _READONLY_GIT_ALLOW)
    def test_readonly_git_never_gated(self, command: str) -> None:
        assert handle_enforce_orchestrator_boundary(_main_bash(command)) is False

    @pytest.mark.parametrize("command", _GIT_PUSH_DENY)
    def test_anti_vacuous_regex_matches_git_push(self, command: str) -> None:
        # The pre-fix denylist carried no git verb — this row would have
        # gone GREEN (allowed) before the fix. Pinning the regex match
        # proves the new arm is what gates it, not an unrelated pattern.
        assert _ORCHESTRATOR_HEAVY_BASH_RE.search(command) is not None


class TestTargetedPytestAllowed:
    """#1825 — a single/targeted pytest run is allowed; only the suite denies.

    The pre-fix ``_PYTEST_VERB_RE`` matched the bare verb, so ``pytest -k
    <expr>`` and ``pytest path::Test::test`` over-blocked. The fix exempts
    a targeted run (``-k``/``::``/specific ``*.py`` file) while keeping a
    bare/whole-suite ``pytest`` and ``pytest <dir>/`` gated.
    """

    @pytest.mark.parametrize("command", _TARGETED_PYTEST_ALLOW)
    def test_targeted_pytest_allowed(self, command: str) -> None:
        assert handle_enforce_orchestrator_boundary(_main_bash(command)) is False

    @pytest.mark.parametrize("command", _WHOLE_SUITE_PYTEST_DENY)
    def test_whole_suite_pytest_denied(self, command: str) -> None:
        assert handle_enforce_orchestrator_boundary(_main_bash(command)) is True

    def test_targeted_pytest_with_other_heavy_arm_still_denied(self) -> None:
        # A targeted pytest does NOT vouch for a chained heavy command.
        assert handle_enforce_orchestrator_boundary(_main_bash("pytest -k foo && npm install")) is True

    def test_chained_targeted_then_whole_suite_denied(self) -> None:
        # Every pytest segment must be targeted; a trailing bare suite denies.
        assert handle_enforce_orchestrator_boundary(_main_bash("pytest -k foo ; pytest")) is True


class TestForegroundAgentBoundary:
    """The #1733 default-ON foreground-Agent boundary (deny + off-ramps)."""

    def test_foreground_agent_dispatch_denied_by_default(self, clean_home: Path) -> None:
        # No config at all → the gate is ON by its #1733 default → a bare
        # foreground main-agent Agent dispatch is denied.
        data = {"tool_name": "Agent", "tool_input": {"description": "implement X", "run_in_background": False}}
        assert handle_enforce_orchestrator_boundary(data) is True

    def test_foreground_agent_dispatch_allowed_in_background(self) -> None:
        data = {"tool_name": "Agent", "tool_input": {"description": "implement X", "run_in_background": True}}
        assert handle_enforce_orchestrator_boundary(data) is False

    def test_foreground_agent_dispatch_allowed_when_kill_switch_set(self, clean_home: Path) -> None:
        (clean_home / ".teatree.toml").write_text(
            "[teatree]\norchestrator_boundary_agent_gate_enabled = false\n", encoding="utf-8"
        )
        data = {"tool_name": "Agent", "tool_input": {"description": "implement X", "run_in_background": False}}
        assert handle_enforce_orchestrator_boundary(data) is False


# ── Turn-budget nudge: responsiveness lever, advisory only ──────────────────────


class TestTurnBudgetNudge:
    def test_nudge_never_denies(self) -> None:
        # Advisory handlers return ``None`` — never a deny verdict.
        assert handle_orchestrator_turn_budget_nudge(_main_tool("Read", file_path="x.py")) is None

    def test_no_nudge_below_budget(self, capsys: pytest.CaptureFixture[str]) -> None:
        for _ in range(24):
            handle_orchestrator_turn_budget_nudge(_main_tool("Read", file_path="x.py"))
        assert capsys.readouterr().out.strip() == ""

    def test_nudge_fires_once_at_budget(self, capsys: pytest.CaptureFixture[str]) -> None:
        for _ in range(25):
            handle_orchestrator_turn_budget_nudge(_main_tool("Read", file_path="x.py"))
        out = capsys.readouterr().out.strip()
        payload = json.loads(out)
        assert "orchestrator-responsiveness" in payload["additionalContext"]
        assert "YIELD" in payload["additionalContext"]

    def test_nudge_fires_only_once_per_turn(self, capsys: pytest.CaptureFixture[str]) -> None:
        for _ in range(40):
            handle_orchestrator_turn_budget_nudge(_main_tool("Read", file_path="x.py"))
        # Exactly one JSON object emitted across the whole over-budget turn.
        lines = [ln for ln in capsys.readouterr().out.splitlines() if ln.strip()]
        assert len(lines) == 1

    def test_reset_rearms_the_nudge_next_turn(self, capsys: pytest.CaptureFixture[str]) -> None:
        for _ in range(25):
            handle_orchestrator_turn_budget_nudge(_main_tool("Read", file_path="x.py"))
        capsys.readouterr()  # drain the first nudge
        handle_reset_turn_tool_budget({"session_id": "s-corpus"})
        for _ in range(25):
            handle_orchestrator_turn_budget_nudge(_main_tool("Read", file_path="x.py"))
        assert json.loads(capsys.readouterr().out.strip())["additionalContext"]

    def test_orchestration_calls_do_not_count_toward_budget(self, capsys: pytest.CaptureFixture[str]) -> None:
        # 100 AskUserQuestion / dispatch calls never count and never nudge:
        # yielding to the user is itself orchestration.
        for _ in range(100):
            handle_orchestrator_turn_budget_nudge(_main_tool("AskUserQuestion"))
            handle_orchestrator_turn_budget_nudge(_main_tool("Task"))
        assert capsys.readouterr().out.strip() == ""

    def test_subagent_calls_are_exempt(self, capsys: pytest.CaptureFixture[str]) -> None:
        sub = {
            "tool_name": "Read",
            "tool_input": {"file_path": "x.py"},
            "session_id": "s-corpus",
            "agent_id": "a4ad83956ff699aaa",
        }
        for _ in range(100):
            handle_orchestrator_turn_budget_nudge(sub)
        assert capsys.readouterr().out.strip() == ""

    def test_budget_zero_disables_nudge(self, clean_home: Path, capsys: pytest.CaptureFixture[str]) -> None:
        (clean_home / ".teatree.toml").write_text("[teatree]\norchestrator_turn_budget = 0\n", encoding="utf-8")
        for _ in range(200):
            handle_orchestrator_turn_budget_nudge(_main_tool("Read", file_path="x.py"))
        assert capsys.readouterr().out.strip() == ""

    def test_custom_budget_respected(self, clean_home: Path, capsys: pytest.CaptureFixture[str]) -> None:
        (clean_home / ".teatree.toml").write_text("[teatree]\norchestrator_turn_budget = 5\n", encoding="utf-8")
        for _ in range(4):
            handle_orchestrator_turn_budget_nudge(_main_tool("Read", file_path="x.py"))
        assert capsys.readouterr().out.strip() == ""
        handle_orchestrator_turn_budget_nudge(_main_tool("Read", file_path="x.py"))
        assert json.loads(capsys.readouterr().out.strip())["additionalContext"]

    def test_missing_session_id_is_a_no_op(self, capsys: pytest.CaptureFixture[str]) -> None:
        for _ in range(50):
            handle_orchestrator_turn_budget_nudge({"tool_name": "Read", "tool_input": {}})
        assert capsys.readouterr().out.strip() == ""


class TestWiredInChains:
    def test_reset_handler_wired_in_user_prompt_submit(self) -> None:
        assert handle_reset_turn_tool_budget in router._HANDLERS["UserPromptSubmit"]

    def test_nudge_handler_wired_last_in_pretooluse(self) -> None:
        # Last so it only prints additionalContext on a non-denied call (a
        # deny earlier in the chain short-circuits before it runs).
        chain = router._HANDLERS["PreToolUse"]
        assert handle_orchestrator_turn_budget_nudge is chain[-1]
