# test-path: cross-cutting — tests the headless_authoring_gate PreToolUse handler wired into hook_router.py.
"""The configured headless posture must be ENFORCED, not merely stored (#3883).

``agent_runtime = headless`` says all implementation work runs through the factory. Nothing
made that true: an interactive Claude Code session could edit ``src/``, dispatch ``t3:coder``
sub-agents, and commit — and none of it was refused.

The single most important case in this file is
:meth:`TestTheFactoryIsNeverRefused.test_a_factory_dispatched_sdk_session_is_allowed`. The
factory's own workers run through the Agent SDK with the SAME hook set, so a gate keyed on
"what is being touched" rather than "who is acting" would refuse the very agents meant to do
the implementing — the fix would stop the factory instead of feeding it. Every case here is
paired with its opposite so a uniformly-permissive gate (which would pass the allow-cases)
and a uniformly-restrictive one (which would pass the refuse-cases) both go red.
"""

import json
import os
import sqlite3
from collections.abc import Iterator
from pathlib import Path
from unittest import mock

import pytest

import hooks.scripts.hook_router as router
from hooks.scripts import headless_authoring_gate as gate

#: The env an INTERACTIVE Claude Code CLI session presents. The SDK transport sets
#: ``CLAUDE_CODE_ENTRYPOINT=sdk-py`` and strips ``CLAUDECODE`` from the child env, so these
#: two keys are what separate a human-driven session from every SDK embedding.
_INTERACTIVE_ENV = {"CLAUDE_CODE_ENTRYPOINT": "cli", "CLAUDECODE": "1"}
_SDK_ENV = {"CLAUDE_CODE_ENTRYPOINT": "sdk-py", "CLAUDE_AGENT_SDK_VERSION": "0.2.95"}


def _run_chain(data: dict) -> bool:
    """Run the real PreToolUse handler chain; ``True`` when some gate denied."""
    return any(handler(data) is True for handler in router._HANDLERS["PreToolUse"])


@pytest.fixture
def engaged_session(tmp_path: Path) -> Iterator[Path]:
    """A teatree-ENGAGED interactive session under the headless posture, in a main clone."""
    with (
        mock.patch.object(router, "STATE_DIR", tmp_path),
        mock.patch.object(router, "_teatree_engaged", return_value=True),
        mock.patch.object(gate, "_posture_is_headless", return_value=True),
        mock.patch.object(gate, "_path_is_in_live_worktree", return_value=False),
        mock.patch.object(gate, "_targets_teatree_repo", return_value=True),
        mock.patch.dict(os.environ, _INTERACTIVE_ENV, clear=False),
    ):
        yield tmp_path


def _edit(path: str = "/repo/src/teatree/config/resolution.py") -> dict:
    return {"session_id": "s1", "tool_name": "Edit", "tool_input": {"file_path": path, "new_string": "x"}}


def _dispatch(subagent: str = "t3:coder") -> dict:
    return {
        "session_id": "s1",
        "tool_name": "Agent",
        "tool_input": {"subagent_type": subagent, "prompt": "implement it", "run_in_background": True},
    }


class TestTheInteractiveSessionIsRefused:
    def test_an_edit_to_teatree_source_is_refused(self, engaged_session: Path, capsys) -> None:
        assert _run_chain(_edit()) is True

    def test_the_refusal_names_the_factory_route(self, engaged_session: Path, capsys) -> None:
        _run_chain(_edit())
        payload = json.loads(capsys.readouterr().out.strip().splitlines()[-1])
        reason = payload["hookSpecificOutput"]["permissionDecisionReason"]
        # A gate that says "no" without saying "do this instead" produces an agent that
        # retries creatively, which is worse than the thing being refused.
        assert "issue" in reason.lower()

    def test_dispatching_an_implementation_subagent_is_refused(self, engaged_session: Path, capsys) -> None:
        assert _run_chain(_dispatch("t3:coder")) is True


class TestTheFactoryIsNeverRefused:
    def test_a_factory_dispatched_sdk_session_is_allowed(self, tmp_path: Path) -> None:
        # THE load-bearing assertion. A wrong refusal here halts the whole factory and
        # every SDK consumer; a wrong allow costs one hand-written edit a human notices.
        with (
            mock.patch.object(router, "STATE_DIR", tmp_path),
            mock.patch.object(router, "_teatree_engaged", return_value=True),
            mock.patch.object(gate, "_posture_is_headless", return_value=True),
            mock.patch.object(gate, "_path_is_in_live_worktree", return_value=False),
            mock.patch.object(gate, "_targets_teatree_repo", return_value=True),
            mock.patch.dict(os.environ, _SDK_ENV, clear=False),
        ):
            os.environ.pop("CLAUDECODE", None)
            assert _run_chain(_edit()) is False
            assert _run_chain(_dispatch("t3:coder")) is False

    def test_an_unreadable_lane_signal_allows(self, tmp_path: Path) -> None:
        # Fail OPEN — the inverse of every other gate in this repo, deliberately. An
        # "unknown" verdict is not evidence of an interactive session.
        with (
            mock.patch.object(router, "STATE_DIR", tmp_path),
            mock.patch.object(router, "_teatree_engaged", return_value=True),
            mock.patch.object(gate, "_posture_is_headless", return_value=True),
            mock.patch.object(gate, "_path_is_in_live_worktree", return_value=False),
            mock.patch.object(gate, "_targets_teatree_repo", return_value=True),
            mock.patch.dict(os.environ, {}, clear=False),
        ):
            for key in ("CLAUDE_CODE_ENTRYPOINT", "CLAUDECODE", "CLAUDE_AGENT_SDK_VERSION"):
                os.environ.pop(key, None)
            assert _run_chain(_edit()) is False

    def test_an_unreadable_posture_allows(self, engaged_session: Path) -> None:
        with mock.patch.object(gate, "_posture_is_headless", return_value=None):
            assert _run_chain(_edit()) is False


class TestTheCoordinatorRoleStaysAvailable:
    def test_reading_and_searching_are_not_refused(self, engaged_session: Path) -> None:
        for command in ("git log --oneline -5", "rg 'autonomy' src/", "t3 doctor check"):
            data = {"session_id": "s1", "tool_name": "Bash", "tool_input": {"command": command}}
            assert _run_chain(data) is False, command

    def test_review_merge_and_issue_filing_are_not_refused(self, engaged_session: Path) -> None:
        for command in (
            "t3 teatree ticket merge 42",
            "t3 teatree ticket clear 42",
            "gh issue create --title x --body y",
            "gh issue comment 42 --body y",
        ):
            data = {"session_id": "s1", "tool_name": "Bash", "tool_input": {"command": command}}
            assert _run_chain(data) is False, command

    def test_host_operations_the_factory_cannot_do_are_not_refused(self, engaged_session: Path) -> None:
        # Dogfooding REQUIRES running t3 commands that mutate host state.
        for command in ("t3 teatree workspace reclaim-disk", "t3 teatree workspace clean-all"):
            data = {"session_id": "s1", "tool_name": "Bash", "tool_input": {"command": command}}
            assert _run_chain(data) is False, command

    def test_a_non_implementation_subagent_dispatch_is_not_refused(self, engaged_session: Path) -> None:
        for subagent in ("Explore", "t3:reviewer", "t3:followup", "general-purpose"):
            assert _run_chain(_dispatch(subagent)) is False, subagent


class TestTheCarveOutsHold:
    def test_work_already_in_flight_in_a_live_worktree_is_not_refused(self, engaged_session: Path) -> None:
        # A t3-managed worktree exists only because a ticket started there. Handing that
        # back to the factory means reconciling state that is cheaper to finish in place;
        # only NEW work is refused.
        with mock.patch.object(gate, "_path_is_in_live_worktree", return_value=True):
            assert _run_chain(_edit("/wt/3873/src/teatree/x.py")) is False

    def test_the_same_edit_without_a_live_checkout_is_refused(self, engaged_session: Path) -> None:
        # The foil that proves the carve-out discriminates rather than swallowing the gate.
        assert _run_chain(_edit("/wt/3873/src/teatree/x.py")) is True

    def test_an_audited_override_unblocks_exactly_one_action(self, engaged_session: Path, tmp_path: Path) -> None:
        allowed = {
            "session_id": "s1",
            "tool_name": "Edit",
            "tool_input": {
                "file_path": "/repo/src/teatree/x.py",
                "new_string": "fix [headless-authoring-ok: factory is down, restoring it]",
            },
        }
        assert _run_chain(allowed) is False
        # Exactly one: the NEXT call, without the token, is refused again.
        assert _run_chain(_edit()) is True

    def test_the_override_is_recorded_with_its_reason(self, engaged_session: Path) -> None:
        data = {
            "session_id": "s1",
            "tool_name": "Edit",
            "tool_input": {"file_path": "/repo/src/teatree/x.py", "new_string": "[headless-authoring-ok: emergency]"},
        }
        _run_chain(data)
        audit = engaged_session / "s1.authoring-overrides"
        assert audit.is_file()
        assert "emergency" in audit.read_text(encoding="utf-8")

    def test_an_empty_override_reason_does_not_unblock(self, engaged_session: Path) -> None:
        data = {
            "session_id": "s1",
            "tool_name": "Edit",
            "tool_input": {"file_path": "/repo/src/teatree/x.py", "new_string": "[headless-authoring-ok: ]"},
        }
        assert _run_chain(data) is True


class TestDefaultOffIsPreserved:
    def test_nothing_is_refused_when_the_posture_is_not_headless(self, engaged_session: Path) -> None:
        with mock.patch.object(gate, "_posture_is_headless", return_value=False):
            assert _run_chain(_edit()) is False
            assert _run_chain(_dispatch("t3:coder")) is False

    def test_an_unengaged_session_is_never_refused(self, engaged_session: Path) -> None:
        # A colleague cloning the repo has not opted in and must feel nothing.
        with mock.patch.object(router, "_teatree_engaged", return_value=False):
            assert _run_chain(_edit()) is False

    def test_an_edit_outside_the_teatree_repo_is_not_refused(self, engaged_session: Path) -> None:
        with mock.patch.object(gate, "_targets_teatree_repo", return_value=False):
            assert _run_chain(_edit("/other/project/src/app.py")) is False

    def test_the_kill_switch_disables_the_gate(self, engaged_session: Path) -> None:
        with mock.patch.object(gate, "_gate_enabled", return_value=False):
            assert _run_chain(_edit()) is False


class TestThePostureReadIsRealRatherThanAlwaysUnreadable:
    """The posture reader, exercised for real rather than mocked.

    The gate is inert if ``_posture_is_headless`` always raises — and every other case here
    mocks it, so nothing above would notice. These run the REAL reader against a real control
    DB, so a wrong call signature (which fails open and disables the gate silently) turns this
    red instead of passing unnoticed.
    """

    @staticmethod
    def _control_db(tmp_path: Path, value: str) -> Path:
        db = tmp_path / "db.sqlite3"
        conn = sqlite3.connect(db)
        with conn:
            conn.execute("CREATE TABLE teatree_config_setting (scope TEXT, key TEXT, value TEXT)")
            conn.execute(
                "INSERT INTO teatree_config_setting VALUES (?, ?, ?)", ("", "agent_runtime", json.dumps(value))
            )
        conn.close()
        return db

    def test_a_stored_headless_runtime_reads_as_headless(self, tmp_path: Path) -> None:
        db = self._control_db(tmp_path, "headless")
        with mock.patch.dict(os.environ, {"T3_CONFIG_DB": str(db), "T3_OVERLAY_NAME": ""}):
            assert gate._posture_is_headless() is True

    def test_a_stored_interactive_runtime_reads_as_not_headless(self, tmp_path: Path) -> None:
        db = self._control_db(tmp_path, "interactive")
        with mock.patch.dict(os.environ, {"T3_CONFIG_DB": str(db), "T3_OVERLAY_NAME": ""}):
            assert gate._posture_is_headless() is False

    def test_an_absent_store_is_unreadable_rather_than_a_verdict(self, tmp_path: Path) -> None:
        with mock.patch.dict(os.environ, {"T3_CONFIG_DB": str(tmp_path / "missing.sqlite3")}):
            assert gate._posture_is_headless() is None
