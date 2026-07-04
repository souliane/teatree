import pytest
from pydantic_ai.exceptions import ApprovalRequired
from pydantic_ai.tools import ToolDefinition

from teatree.agents.lane_b.gating import hard_deny_reason, make_soft_gate_predicate, raise_if_soft_gated
from teatree.core.gates.main_clone_guard import find_main_clone_git_mutation


class TestHardDenyReason:
    def test_main_clone_mutation_is_denied(self) -> None:
        for command in ("git reset --hard HEAD~1", "git checkout my-feature", "git stash pop"):
            assert hard_deny_reason("shell", {"command": command}) is not None

    def test_safe_git_is_allowed(self) -> None:
        for command in ("git fetch origin", "git checkout main", "git worktree add ../wt origin/main", "git status"):
            assert hard_deny_reason("shell", {"command": command}) is None

    def test_non_command_tool_with_clean_text_is_allowed(self) -> None:
        assert hard_deny_reason("read_file", {"path": "src/app.py"}) is None

    def test_shares_the_same_core_classifier_as_lane_a(self) -> None:
        # Parity by construction: the Lane-B evaluator's main-clone verdict is the
        # SAME core classifier Lane A's PreToolUse hook wraps. For any command,
        # the two must agree on deny-vs-allow.
        protected = frozenset({"main", "master", "develop", "development", "release"})
        for command in (
            "git reset --hard",
            "git checkout feature-x",
            "git fetch origin",
            "git checkout main",
            "git restore src/a.py",
        ):
            lane_b_denied = hard_deny_reason("shell", {"command": command}) is not None
            core_finding = find_main_clone_git_mutation(command, default_branch=None, protected_branches=protected)
            assert lane_b_denied == (core_finding is not None)


class TestSoftGate:
    def test_predicate_matches_only_gated_names(self) -> None:
        predicate = make_soft_gate_predicate(frozenset({"shell"}))
        assert predicate(None, _def("shell"), {}) is True
        assert predicate(None, _def("read_file"), {}) is False

    def test_raise_if_soft_gated_raises_approval_required(self) -> None:
        with pytest.raises(ApprovalRequired):
            raise_if_soft_gated("shell", frozenset({"shell"}))

    def test_ungated_name_does_not_raise(self) -> None:
        raise_if_soft_gated("read_file", frozenset({"shell"}))  # must not raise


def _def(name: str) -> ToolDefinition:
    return ToolDefinition(name=name)
