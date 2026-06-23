"""Pure-logic unit tests for the plan-before-code gate helpers (#2425).

The change-vs-read Bash classifier, the gated-tool predicate, and the per-call
``[skip-plan-gate: <reason>]`` escape scanner live in
``hooks/scripts/plan_edit_gate.py`` (factored out of the shrink-only
``hook_router`` god-module). They are pure functions over dicts/strings — no DB,
no I/O — so they are unit-tested directly here. The end-to-end gate behaviour
(real git repo + Worktree row + ``handle_block_edit_before_planned``) is in
``tests/teatree_core/test_hook_router_block_edit_before_planned.py``.
"""

from hooks.scripts import plan_edit_gate


class TestIsChangeMakingBash:
    """Branch coverage for the change-vs-read Bash classifier."""

    def test_git_write_verbs_are_change_making(self) -> None:
        for command in (
            "git commit -m x",
            "git push origin main",
            "git merge feature",
            "git rebase main",
            "git cherry-pick abc123",
            "git am < patch",
        ):
            assert plan_edit_gate.is_change_making_bash(command) is True, command

    def test_pr_and_mr_writes_are_change_making(self) -> None:
        assert plan_edit_gate.is_change_making_bash("gh pr create --fill") is True
        assert plan_edit_gate.is_change_making_bash("gh pr merge 42") is True
        assert plan_edit_gate.is_change_making_bash("glab mr create") is True
        assert plan_edit_gate.is_change_making_bash("glab mr merge 42") is True

    def test_chained_change_after_cd_is_change_making(self) -> None:
        assert plan_edit_gate.is_change_making_bash("cd repo && git push") is True

    def test_read_only_commands_are_not_change_making(self) -> None:
        for command in (
            "git status",
            "git log --oneline -5",
            "git diff HEAD",
            "git show HEAD",
            "gh pr view 42",
            "glab mr view 42",
            "ls -la",
            "grep -r foo src",
        ):
            assert plan_edit_gate.is_change_making_bash(command) is False, command


class TestPlanGateAppliesToTool:
    """Branch coverage for the gated-set predicate."""

    def test_edit_and_write_always_apply(self) -> None:
        assert plan_edit_gate.plan_gate_applies_to_tool({"tool_name": "Edit", "tool_input": {}}) is True
        assert plan_edit_gate.plan_gate_applies_to_tool({"tool_name": "Write", "tool_input": {}}) is True

    def test_change_making_bash_applies_read_only_does_not(self) -> None:
        push = {"tool_name": "Bash", "tool_input": {"command": "git push"}}
        status = {"tool_name": "Bash", "tool_input": {"command": "git status"}}
        assert plan_edit_gate.plan_gate_applies_to_tool(push) is True
        assert plan_edit_gate.plan_gate_applies_to_tool(status) is False

    def test_non_gated_tool_does_not_apply(self) -> None:
        assert plan_edit_gate.plan_gate_applies_to_tool({"tool_name": "Read", "tool_input": {}}) is False

    def test_bash_with_non_dict_tool_input_does_not_apply(self) -> None:
        assert plan_edit_gate.plan_gate_applies_to_tool({"tool_name": "Bash", "tool_input": None}) is False


class TestSkipPlanGateToken:
    """Branch coverage for the per-call ``[skip-plan-gate: <reason>]`` escape scanner."""

    def test_token_in_bash_command_returns_reason(self) -> None:
        data = {"tool_input": {"command": "git commit -m 'x [skip-plan-gate: trivial bump]'"}}
        assert plan_edit_gate.skip_plan_gate_token(data) == "trivial bump"

    def test_token_in_edit_new_string_returns_reason(self) -> None:
        data = {"tool_input": {"new_string": "y [skip-plan-gate: docs typo]"}}
        assert plan_edit_gate.skip_plan_gate_token(data) == "docs typo"

    def test_empty_reason_token_returns_none(self) -> None:
        data = {"tool_input": {"command": "git push [skip-plan-gate: ]"}}
        assert plan_edit_gate.skip_plan_gate_token(data) is None

    def test_no_token_returns_none(self) -> None:
        assert plan_edit_gate.skip_plan_gate_token({"tool_input": {"command": "git commit -m x"}}) is None

    def test_non_dict_tool_input_returns_none(self) -> None:
        assert plan_edit_gate.skip_plan_gate_token({"tool_input": None}) is None

    def test_token_beyond_scan_limit_is_ignored(self) -> None:
        buried = "x" * 600 + "[skip-plan-gate: too far]"
        assert plan_edit_gate.skip_plan_gate_token({"tool_input": {"command": buried}}) is None
