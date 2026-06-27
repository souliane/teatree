"""Tests for the orchestrator-investigation-boundary WARN nudge (#1442).

The heavy-Bash gate (#115) DENIES only the load-bearing slice of the
orchestrator-decides / loop-executes topology (no long/heavy foreground Bash).
This nudge enforces the BROADER boundary the heavy-Bash gate leaves to skill
prose: the loop-owner orchestrator does ONLY orchestration and delegates
investigation / diagnosis / fixing / code edits / git archaeology / test runs to
a sub-agent in a worktree. It is the broad WARN-only complement to the narrow
Agent-dispatch DENY.

It is a WARN, never a deny — the only never-lockout-safe enforcement for a
boundary this broad. It fires ONLY for the live loop-owner session (not
sub-agents, not a non-owner interactive session), and is suppressed by a per-call
``[orchestration-ok: <reason>]`` token or the out-of-repo kill-switch ``[teatree]
orchestrator_investigation_gate_enabled = false``.
"""

import ast
import contextlib
import io
import sys
from collections.abc import Iterator
from datetime import timedelta
from pathlib import Path
from unittest import mock

import pytest
from django.test import TestCase
from django.utils import timezone

import hooks.scripts.hook_router as router
import hooks.scripts.orchestrator_investigation_gate as gate
from hooks.scripts.orchestrator_investigation_gate import (
    _investigation_signal,
    _orchestrator_investigation_gate_enabled,
    _session_is_loop_owner,
    handle_enforce_orchestrator_investigation_boundary,
)
from teatree.core.models import LoopLease

_OWNER = "sess-owner"


@contextlib.contextmanager
def _capture_stderr() -> Iterator[io.StringIO]:
    """Capture ``sys.stderr`` writes inside a Django ``TestCase`` (no ``capsys``)."""
    buffer = io.StringIO()
    original = sys.stderr
    sys.stderr = buffer
    try:
        yield buffer
    finally:
        sys.stderr = original


@pytest.fixture(autouse=True)
def _gate_enabled_home(tmp_path_factory: pytest.TempPathFactory, monkeypatch: pytest.MonkeyPatch) -> None:
    """Point ``Path.home`` at a clean tmp dir so the gate is ON by default."""
    home = tmp_path_factory.mktemp("home")
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: home))


def _owner_call(tool_name: str, **tool_input: object) -> dict:
    return {"session_id": _OWNER, "tool_name": tool_name, "tool_input": tool_input}


def _owner_bash(command: str) -> dict:
    return _owner_call("Bash", command=command)


def _subagent_bash(command: str) -> dict:
    return {
        "session_id": _OWNER,
        "tool_name": "Bash",
        "tool_input": {"command": command},
        "agent_id": "a4ad83956ff699aaa",
        "agent_type": "general-purpose",
    }


class TestInvestigationSignalClassification:
    """``_investigation_signal`` names the investigation/fix shape, or ``None``.

    Pure-logic level (no DB / owner check) — the WHAT-looks-like-work classifier
    the nudge sits on.
    """

    @pytest.mark.parametrize(
        "command",
        [
            "git show HEAD~3",
            "git blame src/teatree/core/loop.py",
            "git bisect start",
            "git log -S needle -- src/",
            "git log --patch src/teatree/core/loop.py",
            "git log -G regex",
            "git log --follow src/teatree/core/loop.py",
            "git diff HEAD~1",
            "git diff origin/main",
            "git diff main..feature",
            "gh run view 12345",
            "gh run list --workflow ci",
            "gh api repos/o/r/commits",
            "glab ci view",
            "glab pipeline list",
            "gh pr checks 42 --watch",
            "gh pr diff 42",
            "glab mr diff 7",
        ],
    )
    def test_investigation_bash_is_flagged(self, command: str) -> None:
        assert _investigation_signal(_owner_bash(command)) == "a deep git/CI investigation command"

    @pytest.mark.parametrize(
        "command",
        [
            "git status",
            "git diff --stat",
            "git diff --name-only",
            "git log --oneline -5",
            "gh pr view 42",
            "gh pr view 42 --json state",
            "gh pr list --author @me",
            "glab mr view 7",
            "glab mr list",
            "cat src/teatree/core/loop.py",
            "ls -la",
            "grep -rn TODO src/",
            "t3 loop status",
            "t3 teatree checking show",
            "t3 teatree ticket merge 7 --human-authorized owner",
            "t3 teatree gate disable",
            "t3 teatree gate fail-open enable",
            "t3 teatree db migrate",
        ],
    )
    def test_orientation_bash_is_not_flagged(self, command: str) -> None:
        assert _investigation_signal(_owner_bash(command)) is None

    def test_foreground_pytest_is_flagged(self) -> None:
        assert _investigation_signal(_owner_bash("uv run pytest --no-cov -q")) == "a foreground test run"

    @pytest.mark.parametrize("tool_name", ["Edit", "Write", "NotebookEdit"])
    def test_edit_write_is_flagged(self, tool_name: str) -> None:
        signal = _investigation_signal(_owner_call(tool_name, file_path="src/x.py", new_string="y"))
        assert signal == "an Edit/Write that mutates a file"

    @pytest.mark.parametrize("tool_name", ["Read", "Grep", "Glob"])
    def test_read_only_tools_are_not_flagged(self, tool_name: str) -> None:
        assert _investigation_signal(_owner_call(tool_name, file_path="src/x.py")) is None

    @pytest.mark.parametrize("command", [None, 123, ["pytest"]])
    def test_non_str_command_is_not_flagged(self, command: object) -> None:
        assert _investigation_signal({"tool_name": "Bash", "tool_input": {"command": command}}) is None

    def test_unknown_tool_is_not_flagged(self) -> None:
        assert _investigation_signal({"tool_name": "WebFetch", "tool_input": {}}) is None


class TestNudgeIsWarnOnlyNeverDenies:
    """The handler NEVER returns ``True`` — it cannot lock out the orchestrator.

    A loop-owner running investigation work gets a stderr nudge and the call
    PROCEEDS (``False``). This is the strongest possible never-lockout guarantee:
    the gate has no deny path at all.
    """

    @pytest.fixture(autouse=True)
    def _is_owner(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(gate, "_session_is_loop_owner", lambda _sid: True)

    @pytest.mark.parametrize(
        "data",
        [
            _owner_bash("git show HEAD"),
            _owner_bash("git log -S secret"),
            _owner_bash("gh run view 1"),
            _owner_bash("uv run pytest"),
            _owner_call("Edit", file_path="src/x.py", new_string="patch"),
            _owner_call("Write", file_path="src/x.py", content="new file"),
        ],
    )
    def test_investigation_never_denies(self, data: dict) -> None:
        assert handle_enforce_orchestrator_investigation_boundary(data) is False

    def test_investigation_emits_a_stderr_nudge(self, capsys: pytest.CaptureFixture[str]) -> None:
        assert handle_enforce_orchestrator_investigation_boundary(_owner_bash("git show HEAD")) is False
        err = capsys.readouterr().err
        assert "[orchestration-boundary]" in err
        assert "sub-agent" in err
        assert "[orchestration-ok:" in err

    def test_orientation_call_emits_no_nudge(self, capsys: pytest.CaptureFixture[str]) -> None:
        assert handle_enforce_orchestrator_investigation_boundary(_owner_bash("git status")) is False
        assert capsys.readouterr().err.strip() == ""


class TestNudgeScopedToLoopOwnerOnly(TestCase):
    """The nudge fires ONLY for the live loop-owner session.

    A real ``LoopLease`` row drives ``_session_is_loop_owner`` (via the canonical
    ``ownership_status`` predicate) — RED/GREEN proof that a non-owner interactive
    session inspects freely while the owner is nudged.
    """

    @staticmethod
    def _claim_owner(session_id: str, *, live: bool = True) -> None:
        expires = timezone.now() + (timedelta(hours=1) if live else timedelta(seconds=-5))
        LoopLease.objects.create(
            name="loop-owner",
            owner=session_id,
            session_id=session_id,
            owner_pid=None,
            acquired_at=timezone.now(),
            lease_expires_at=expires,
        )

    def test_owner_session_is_recognized(self) -> None:
        self._claim_owner(_OWNER)
        assert _session_is_loop_owner(_OWNER) is True

    def test_non_owner_session_is_not_owner(self) -> None:
        self._claim_owner(_OWNER)
        assert _session_is_loop_owner("sess-other") is False

    def test_expired_lease_is_not_owner(self) -> None:
        self._claim_owner(_OWNER, live=False)
        assert _session_is_loop_owner(_OWNER) is False

    def test_no_lease_is_not_owner(self) -> None:
        assert _session_is_loop_owner(_OWNER) is False

    def test_empty_session_is_not_owner(self) -> None:
        self._claim_owner("")
        assert _session_is_loop_owner("") is False

    def test_db_error_fails_open_to_not_owner(self) -> None:
        # A DB hiccup must never make the nudge fire on a non-owner — fail OPEN
        # to not-owner (silence is the safe direction for a steering nudge).
        self._claim_owner(_OWNER)
        with mock.patch.object(LoopLease.objects, "ownership_status", side_effect=RuntimeError("db down")):
            assert _session_is_loop_owner(_OWNER) is False

    def test_failed_bootstrap_is_not_owner(self) -> None:
        self._claim_owner(_OWNER)
        with mock.patch.object(gate, "bootstrap_teatree_django", return_value=False):
            assert _session_is_loop_owner(_OWNER) is False

    def test_owner_investigation_emits_nudge(self) -> None:
        self._claim_owner(_OWNER)
        with _capture_stderr() as err:
            verdict = handle_enforce_orchestrator_investigation_boundary(_owner_bash("git show HEAD"))
        assert verdict is False
        assert "[orchestration-boundary]" in err.getvalue()

    def test_non_owner_investigation_is_silent(self) -> None:
        self._claim_owner(_OWNER)
        with _capture_stderr() as err:
            verdict = handle_enforce_orchestrator_investigation_boundary(
                {"session_id": "sess-other", "tool_name": "Bash", "tool_input": {"command": "git show HEAD"}}
            )
        assert verdict is False
        assert err.getvalue().strip() == ""


class TestSubagentNeverNudged:
    """A sub-agent (non-empty ``agent_id``) is the hands — never nudged.

    Detection uses ``agent_id`` (the #115 signal), so a sub-agent investigating or
    fixing in its own worktree is exempt even before the owner check runs (no DB
    needed).
    """

    def test_subagent_investigation_is_silent(self, capsys: pytest.CaptureFixture[str]) -> None:
        assert handle_enforce_orchestrator_investigation_boundary(_subagent_bash("git show HEAD")) is False
        assert capsys.readouterr().err.strip() == ""


class TestOrchestrationOkEscapeHatch:
    """``[orchestration-ok: <reason>]`` suppresses the nudge for a genuine read."""

    @pytest.fixture(autouse=True)
    def _is_owner(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(gate, "_session_is_loop_owner", lambda _sid: True)

    def test_token_in_bash_suppresses_nudge(self, capsys: pytest.CaptureFixture[str]) -> None:
        data = _owner_bash("git show HEAD [orchestration-ok: routing the next dispatch]")
        assert handle_enforce_orchestrator_investigation_boundary(data) is False
        assert capsys.readouterr().err.strip() == ""

    def test_token_in_edit_content_suppresses_nudge(self, capsys: pytest.CaptureFixture[str]) -> None:
        data = _owner_call("Edit", file_path="src/x.py", new_string="x  [orchestration-ok: sanctioned memory write]")
        assert handle_enforce_orchestrator_investigation_boundary(data) is False
        assert capsys.readouterr().err.strip() == ""

    def test_empty_reason_does_not_suppress(self, capsys: pytest.CaptureFixture[str]) -> None:
        data = _owner_bash("git show HEAD [orchestration-ok: ]")
        assert handle_enforce_orchestrator_investigation_boundary(data) is False
        assert "[orchestration-boundary]" in capsys.readouterr().err


class TestGateKillSwitch:
    def test_disabled_via_toml_suppresses_nudge(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        (tmp_path / ".teatree.toml").write_text(
            "[teatree]\norchestrator_investigation_gate_enabled = false\n", encoding="utf-8"
        )
        monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path))
        monkeypatch.setattr(gate, "_session_is_loop_owner", lambda _sid: True)
        assert _orchestrator_investigation_gate_enabled() is False
        assert handle_enforce_orchestrator_investigation_boundary(_owner_bash("git show HEAD")) is False
        assert capsys.readouterr().err.strip() == ""

    def test_enabled_by_default_when_key_absent(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        (tmp_path / ".teatree.toml").write_text("[teatree]\n", encoding="utf-8")
        monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path))
        assert _orchestrator_investigation_gate_enabled() is True

    def test_enabled_when_config_missing(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path))
        assert _orchestrator_investigation_gate_enabled() is True

    def test_enabled_on_broken_toml(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        (tmp_path / ".teatree.toml").write_text("this is not = valid [[[", encoding="utf-8")
        monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path))
        assert _orchestrator_investigation_gate_enabled() is True


class TestRegisteredAsWarnOnly:
    def test_handler_is_in_pretooluse_chain(self) -> None:
        assert handle_enforce_orchestrator_investigation_boundary in router._HANDLERS["PreToolUse"]

    def test_handler_never_reaches_a_deny_emitter(self) -> None:
        # Structural pin: the nudge must never call emit_pretooluse_deny nor
        # _fail_open_or_deny, so it can never appear as a lockout-prone deny gate.
        # We assert at BOTH scopes — the handler function AND the whole module —
        # so no future helper in this module can introduce a deny path.
        tree = ast.parse(Path(gate.__file__).read_text(encoding="utf-8"))
        deny_emitters = {"emit_pretooluse_deny", "_fail_open_or_deny"}

        module_calls = {
            node.func.id for node in ast.walk(tree) if isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
        }
        assert deny_emitters.isdisjoint(module_calls)

        handler = next(
            n
            for n in ast.walk(tree)
            if isinstance(n, ast.FunctionDef) and n.name == "handle_enforce_orchestrator_investigation_boundary"
        )
        handler_calls = {
            node.func.id for node in ast.walk(handler) if isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
        }
        assert deny_emitters.isdisjoint(handler_calls)

    def test_handler_returns_false_on_every_path(self) -> None:
        # Every ``return`` in the handler is a bare ``return False`` — there is no
        # truthy (deny) return anywhere, complementing the AST no-deny pin above.
        tree = ast.parse(Path(gate.__file__).read_text(encoding="utf-8"))
        handler = next(
            n
            for n in ast.walk(tree)
            if isinstance(n, ast.FunctionDef) and n.name == "handle_enforce_orchestrator_investigation_boundary"
        )
        returns = [n for n in ast.walk(handler) if isinstance(n, ast.Return)]
        assert returns
        assert all(isinstance(r.value, ast.Constant) and r.value.value is False for r in returns)
