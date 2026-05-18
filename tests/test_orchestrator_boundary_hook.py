"""Tests for the orchestrator-execution-boundary gate (#836 §17.6 gate 2).

The orchestrator (MAIN agent) is delegate-only (BLUEPRINT §17.4 /
§17.8): it dispatches sub-agents and decides merges/clears; it must NOT
itself Edit/Write/Read-sweep/Grep or run mutating Bash. Sub-agents are
the hands that implement and are unaffected. The main-vs-sub-agent
signal is the transcript's ``isSidechain`` marker (Claude Code logs
sub-agent turns with ``isSidechain: true``); the gate fails open when
the signal is unavailable (documented limitation — no false blocks).
"""

import json

import hooks.scripts.hook_router as router
from hooks.scripts.hook_router import (
    _active_turn_is_sidechain,
    _is_orchestration_action,
    handle_enforce_orchestrator_boundary,
)


def _transcript(tmp_path, *, sidechain: bool | None):
    """Write a one-turn transcript; ``sidechain`` None ⇒ omit the key."""
    entry: dict = {"type": "assistant", "message": {"role": "assistant", "content": []}}
    if sidechain is not None:
        entry["isSidechain"] = sidechain
    path = tmp_path / "transcript.jsonl"
    path.write_text(json.dumps(entry) + "\n", encoding="utf-8")
    return str(path)


class TestActiveTurnSidechainSignal:
    def test_subagent_turn_detected(self, tmp_path):
        assert _active_turn_is_sidechain(_transcript(tmp_path, sidechain=True)) is True

    def test_main_agent_turn_detected(self, tmp_path):
        assert _active_turn_is_sidechain(_transcript(tmp_path, sidechain=False)) is False

    def test_no_marker_yields_none(self, tmp_path):
        assert _active_turn_is_sidechain(_transcript(tmp_path, sidechain=None)) is None

    def test_missing_transcript_yields_none(self):
        assert _active_turn_is_sidechain("/no/such/transcript.jsonl") is None

    def test_most_recent_marker_wins(self, tmp_path):
        path = tmp_path / "t.jsonl"
        path.write_text(
            json.dumps({"type": "assistant", "isSidechain": False, "message": {"role": "assistant", "content": []}})
            + "\n"
            + json.dumps({"type": "assistant", "isSidechain": True, "message": {"role": "assistant", "content": []}})
            + "\n",
            encoding="utf-8",
        )
        assert _active_turn_is_sidechain(str(path)) is True


class TestOrchestrationActionAllowList:
    def test_task_dispatch_is_orchestration(self):
        assert _is_orchestration_action({"tool_name": "Task", "tool_input": {}}) is True

    def test_ask_user_question_is_orchestration(self):
        assert _is_orchestration_action({"tool_name": "AskUserQuestion", "tool_input": {}}) is True

    def test_t3_bash_is_orchestration(self):
        data = {"tool_name": "Bash", "tool_input": {"command": "t3 teatree ticket clear 5 slug abc123"}}
        assert _is_orchestration_action(data) is True

    def test_gh_pr_view_is_orchestration(self):
        data = {"tool_name": "Bash", "tool_input": {"command": "gh pr view 42 --json state"}}
        assert _is_orchestration_action(data) is True

    def test_git_status_is_orchestration(self):
        data = {"tool_name": "Bash", "tool_input": {"command": "git status"}}
        assert _is_orchestration_action(data) is True

    def test_edit_is_not_orchestration(self):
        assert _is_orchestration_action({"tool_name": "Edit", "tool_input": {}}) is False

    def test_mutating_bash_is_not_orchestration(self):
        data = {"tool_name": "Bash", "tool_input": {"command": "rm -rf build/ && touch x"}}
        assert _is_orchestration_action(data) is False


class TestMainAgentNonOrchestrationIsBlocked:
    def test_main_agent_edit_is_flagged(self, tmp_path, capsys):
        data = {
            "tool_name": "Edit",
            "tool_input": {"file_path": "/x", "old_string": "a", "new_string": "b"},
            "transcript_path": _transcript(tmp_path, sidechain=False),
        }
        assert handle_enforce_orchestrator_boundary(data) is True
        out = json.loads(capsys.readouterr().out)
        assert out["permissionDecision"] == "deny"
        assert "orchestrator" in out["permissionDecisionReason"]
        assert "delegate-only" in out["permissionDecisionReason"]

    def test_main_agent_write_is_flagged(self, tmp_path):
        data = {
            "tool_name": "Write",
            "tool_input": {"file_path": "/x", "content": "y"},
            "transcript_path": _transcript(tmp_path, sidechain=False),
        }
        assert handle_enforce_orchestrator_boundary(data) is True

    def test_main_agent_investigative_grep_is_flagged(self, tmp_path):
        data = {
            "tool_name": "Grep",
            "tool_input": {"pattern": "TODO"},
            "transcript_path": _transcript(tmp_path, sidechain=False),
        }
        assert handle_enforce_orchestrator_boundary(data) is True

    def test_main_agent_mutating_bash_is_flagged(self, tmp_path):
        data = {
            "tool_name": "Bash",
            "tool_input": {"command": "sed -i 's/a/b/' file.py"},
            "transcript_path": _transcript(tmp_path, sidechain=False),
        }
        assert handle_enforce_orchestrator_boundary(data) is True


class TestSubAgentAndOrchestrationAreAllowed:
    def test_subagent_edit_is_allowed(self, tmp_path):
        data = {
            "tool_name": "Edit",
            "tool_input": {"file_path": "/x", "old_string": "a", "new_string": "b"},
            "transcript_path": _transcript(tmp_path, sidechain=True),
        }
        assert handle_enforce_orchestrator_boundary(data) is False

    def test_main_agent_task_dispatch_is_allowed(self, tmp_path):
        data = {
            "tool_name": "Task",
            "tool_input": {"description": "implement X"},
            "transcript_path": _transcript(tmp_path, sidechain=False),
        }
        assert handle_enforce_orchestrator_boundary(data) is False

    def test_main_agent_t3_merge_is_allowed(self, tmp_path):
        data = {
            "tool_name": "Bash",
            "tool_input": {"command": "t3 teatree ticket merge 7 --human-authorized owner"},
            "transcript_path": _transcript(tmp_path, sidechain=False),
        }
        assert handle_enforce_orchestrator_boundary(data) is False

    def test_unknown_agent_fails_open_not_blocked(self, tmp_path):
        # No isSidechain marker ⇒ cannot tell ⇒ must NOT block (no false block).
        data = {
            "tool_name": "Edit",
            "tool_input": {"file_path": "/x", "old_string": "a", "new_string": "b"},
            "transcript_path": _transcript(tmp_path, sidechain=None),
        }
        assert handle_enforce_orchestrator_boundary(data) is False

    def test_missing_transcript_fails_open(self):
        data = {
            "tool_name": "Edit",
            "tool_input": {"file_path": "/x", "old_string": "a", "new_string": "b"},
            "transcript_path": "",
        }
        assert handle_enforce_orchestrator_boundary(data) is False


class TestRegisteredInChain:
    def test_handler_is_in_pretooluse_chain(self):
        assert handle_enforce_orchestrator_boundary in router._HANDLERS["PreToolUse"]


class TestInvestigativeBashIsBlocked942:
    """#942 — main-agent investigative Bash falls through the allow-list.

    Covers ``cat``/``grep``/``rg``/``awk``/``sed`` of source or fetched
    artifacts. The positive cases below were the recurring failure mode
    the user surfaced in the issue: artifact download + Read sequence
    (``cat /tmp/screenshot.png``) and repo-wide analysis grep
    (``rg TODO src/``, ``grep -r ... src/``). The negative cases must
    keep working because they are the sanctioned orchestrator surface:
    single status checks, piped read-only inspection
    (``gh pr view ... | grep state``), and pure orientation commands
    (``echo``, ``pwd``, ``ls``).
    """

    def test_main_agent_cat_of_fetched_artifact_is_flagged(self, tmp_path):
        data = {
            "tool_name": "Bash",
            "tool_input": {"command": "cat /tmp/screenshot.png"},
            "transcript_path": _transcript(tmp_path, sidechain=False),
        }
        assert handle_enforce_orchestrator_boundary(data) is True

    def test_main_agent_cat_of_source_file_is_flagged(self, tmp_path):
        data = {
            "tool_name": "Bash",
            "tool_input": {"command": "cat src/teatree/foo.py"},
            "transcript_path": _transcript(tmp_path, sidechain=False),
        }
        assert handle_enforce_orchestrator_boundary(data) is True

    def test_main_agent_repo_wide_rg_is_flagged(self, tmp_path):
        data = {
            "tool_name": "Bash",
            "tool_input": {"command": "rg TODO src/"},
            "transcript_path": _transcript(tmp_path, sidechain=False),
        }
        assert handle_enforce_orchestrator_boundary(data) is True

    def test_main_agent_recursive_grep_is_flagged(self, tmp_path):
        data = {
            "tool_name": "Bash",
            "tool_input": {"command": "grep -r TODO src/"},
            "transcript_path": _transcript(tmp_path, sidechain=False),
        }
        assert handle_enforce_orchestrator_boundary(data) is True

    def test_main_agent_head_of_source_is_flagged(self, tmp_path):
        data = {
            "tool_name": "Bash",
            "tool_input": {"command": "head -50 src/teatree/cli/overlay.py"},
            "transcript_path": _transcript(tmp_path, sidechain=False),
        }
        assert handle_enforce_orchestrator_boundary(data) is True

    def test_main_agent_awk_of_file_is_flagged(self, tmp_path):
        data = {
            "tool_name": "Bash",
            "tool_input": {"command": "awk '/foo/{print}' src/teatree/foo.py"},
            "transcript_path": _transcript(tmp_path, sidechain=False),
        }
        assert handle_enforce_orchestrator_boundary(data) is True


class TestSanctionedReadOnlyBashStillAllowed942:
    """Negative cases the #942 fix must preserve unbroken."""

    def test_gh_pr_checks_is_allowed(self, tmp_path):
        data = {
            "tool_name": "Bash",
            "tool_input": {"command": "gh pr checks 42"},
            "transcript_path": _transcript(tmp_path, sidechain=False),
        }
        assert handle_enforce_orchestrator_boundary(data) is False

    def test_gh_pr_view_piped_to_grep_is_allowed(self, tmp_path):
        # The orchestrator regex matches the START of the command. A
        # primary read-only verb piped to grep/cat/jq is still a
        # sanctioned status check.
        data = {
            "tool_name": "Bash",
            "tool_input": {"command": "gh pr view 42 --json state | grep state"},
            "transcript_path": _transcript(tmp_path, sidechain=False),
        }
        assert handle_enforce_orchestrator_boundary(data) is False

    def test_glab_mr_view_piped_to_jq_is_allowed(self, tmp_path):
        data = {
            "tool_name": "Bash",
            "tool_input": {"command": "glab mr view 7 | grep '^state'"},
            "transcript_path": _transcript(tmp_path, sidechain=False),
        }
        assert handle_enforce_orchestrator_boundary(data) is False

    def test_t3_ticket_clear_is_allowed(self, tmp_path):
        data = {
            "tool_name": "Bash",
            "tool_input": {"command": "t3 teatree ticket clear 5 slug abc123 --reviewer-identity x"},
            "transcript_path": _transcript(tmp_path, sidechain=False),
        }
        assert handle_enforce_orchestrator_boundary(data) is False

    def test_git_status_is_allowed(self, tmp_path):
        data = {
            "tool_name": "Bash",
            "tool_input": {"command": "git status"},
            "transcript_path": _transcript(tmp_path, sidechain=False),
        }
        assert handle_enforce_orchestrator_boundary(data) is False

    def test_echo_orientation_is_allowed(self, tmp_path):
        data = {
            "tool_name": "Bash",
            "tool_input": {"command": "echo $T3_REPO"},
            "transcript_path": _transcript(tmp_path, sidechain=False),
        }
        assert handle_enforce_orchestrator_boundary(data) is False

    def test_pwd_orientation_is_allowed(self, tmp_path):
        data = {
            "tool_name": "Bash",
            "tool_input": {"command": "pwd"},
            "transcript_path": _transcript(tmp_path, sidechain=False),
        }
        assert handle_enforce_orchestrator_boundary(data) is False

    def test_ls_orientation_is_allowed(self, tmp_path):
        data = {
            "tool_name": "Bash",
            "tool_input": {"command": "ls -la"},
            "transcript_path": _transcript(tmp_path, sidechain=False),
        }
        assert handle_enforce_orchestrator_boundary(data) is False

    def test_subagent_grep_is_still_allowed(self, tmp_path):
        # Sub-agents implement — they may grep source freely.
        data = {
            "tool_name": "Bash",
            "tool_input": {"command": "grep -r TODO src/"},
            "transcript_path": _transcript(tmp_path, sidechain=True),
        }
        assert handle_enforce_orchestrator_boundary(data) is False
