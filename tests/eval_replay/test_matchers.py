import pytest

from teatree.eval.matchers import (
    assert_final_state_contains,
    assert_final_state_matching,
    assert_no_tool_call_contains,
    assert_no_tool_call_matching,
    assert_tool_call_contains,
    assert_tool_call_matching,
)
from teatree.eval.models import EvalRun, EvalToolCall


def _run(tool_calls: list[EvalToolCall]) -> EvalRun:
    return EvalRun(
        spec_name="t",
        tool_calls=tuple(tool_calls),
        text_blocks=(),
        terminal_reason="success",
        is_error=False,
        raw_stdout="",
        raw_stderr="",
    )


def _run_with_text(*text_blocks: str, terminal_reason: str = "success") -> EvalRun:
    return EvalRun(
        spec_name="t",
        tool_calls=(),
        text_blocks=tuple(text_blocks),
        terminal_reason=terminal_reason,
        is_error=False,
        raw_stdout="",
        raw_stderr="",
    )


class TestAssertToolCallContains:
    def test_passes_when_substring_present(self) -> None:
        run = _run([EvalToolCall(name="Bash", input={"command": "git worktree add ../wt main"}, turn=1)])
        assert_tool_call_contains(run, "Bash", "command", "git worktree add")

    def test_raises_when_substring_absent(self) -> None:
        run = _run([EvalToolCall(name="Bash", input={"command": "ls"}, turn=1)])
        with pytest.raises(AssertionError) as exc_info:
            assert_tool_call_contains(run, "Bash", "command", "git worktree add")
        assert "git worktree add" in str(exc_info.value)
        assert "ls" in str(exc_info.value)

    def test_raises_when_tool_name_does_not_match(self) -> None:
        run = _run([EvalToolCall(name="Read", input={"command": "git worktree add"}, turn=1)])
        with pytest.raises(AssertionError):
            assert_tool_call_contains(run, "Bash", "command", "git worktree add")

    def test_raises_when_no_tool_calls_captured(self) -> None:
        run = _run([])
        with pytest.raises(AssertionError) as exc_info:
            assert_tool_call_contains(run, "Bash", "command", "x")
        assert "no tool calls captured" in str(exc_info.value)


class TestScalarArgCoercion:
    def test_matches_boolean_run_in_background_true(self) -> None:
        # A Bash `run_in_background: true` arg is a bool, not a string; the
        # matcher must compare its str() form so the documented backgrounding
        # escape is pinnable.
        run = _run([EvalToolCall(name="Bash", input={"command": "uv run pytest", "run_in_background": True}, turn=1)])
        assert_tool_call_matching(run, "Bash", "run_in_background", "(?i)true")

    def test_does_not_match_false_run_in_background(self) -> None:
        run = _run([EvalToolCall(name="Bash", input={"command": "ls", "run_in_background": False}, turn=1)])
        with pytest.raises(AssertionError):
            assert_tool_call_matching(run, "Bash", "run_in_background", "(?i)true")

    def test_list_arg_is_searched_via_json(self) -> None:
        # A structured list arg (e.g. AskUserQuestion's `questions`) is
        # JSON-serialized so a regex can search its contents — otherwise a
        # structured-arg tool would be silently unmatchable.
        run = _run(
            [EvalToolCall(name="AskUserQuestion", input={"questions": [{"question": "upstream or overlay?"}]}, turn=1)]
        )
        assert_tool_call_matching(run, "AskUserQuestion", "questions", "(?i)upstream")

    def test_none_arg_is_not_matchable(self) -> None:
        run = _run([EvalToolCall(name="Bash", input={"command": None}, turn=1)])
        with pytest.raises(AssertionError):
            assert_tool_call_matching(run, "Bash", "command", "a")


class TestAssertToolCallMatching:
    def test_passes_when_pattern_present(self) -> None:
        run = _run([EvalToolCall(name="Bash", input={"command": "git worktree add ../wt-42 -b 42-fix main"}, turn=1)])
        assert_tool_call_matching(run, "Bash", "command", r"git worktree add.*-b\s+[0-9]")

    def test_raises_when_pattern_absent(self) -> None:
        run = _run([EvalToolCall(name="Bash", input={"command": "ls"}, turn=1)])
        with pytest.raises(AssertionError) as exc_info:
            assert_tool_call_matching(run, "Bash", "command", r"git worktree add")
        assert "git worktree add" in str(exc_info.value)

    def test_raises_when_no_tool_calls_captured(self) -> None:
        run = _run([])
        with pytest.raises(AssertionError) as exc_info:
            assert_tool_call_matching(run, "Bash", "command", r"x")
        assert "no tool calls captured" in str(exc_info.value)

    def test_raises_when_only_other_tool_matches(self) -> None:
        run = _run([EvalToolCall(name="Read", input={"command": "git worktree add"}, turn=1)])
        with pytest.raises(AssertionError):
            assert_tool_call_matching(run, "Bash", "command", r"git worktree add")


class TestAssertNoToolCallMatching:
    def test_passes_when_pattern_absent(self) -> None:
        run = _run([EvalToolCall(name="Bash", input={"command": "git worktree add"}, turn=1)])
        assert_no_tool_call_matching(run, "Bash", "command", r"Edit.*README\.md")

    def test_passes_when_only_other_tool_matches(self) -> None:
        run = _run([EvalToolCall(name="Read", input={"command": "Edit README.md"}, turn=1)])
        assert_no_tool_call_matching(run, "Bash", "command", r"Edit.*README\.md")

    def test_raises_when_pattern_matches(self) -> None:
        run = _run([EvalToolCall(name="Bash", input={"command": "Edit /path/to/README.md"}, turn=1)])
        with pytest.raises(AssertionError) as exc_info:
            assert_no_tool_call_matching(run, "Bash", "command", r"Edit.*README\.md")
        assert "Edit" in str(exc_info.value)


class TestAssertNoToolCallContains:
    """The substring sibling of ``assert_no_tool_call_matching`` (regex).

    Also the logical negation of ``assert_tool_call_contains``: PASS when NO
    matching call has its arg value containing the substring; raise when at least
    one does.
    """

    def test_passes_when_substring_absent(self) -> None:
        run = _run([EvalToolCall(name="Bash", input={"command": "git commit -m fix"}, turn=1)])
        assert_no_tool_call_contains(run, "Bash", "command", "--no-verify")

    def test_passes_when_only_other_tool_contains_substring(self) -> None:
        run = _run([EvalToolCall(name="Read", input={"command": "git commit --no-verify"}, turn=1)])
        assert_no_tool_call_contains(run, "Bash", "command", "--no-verify")

    def test_passes_when_matching_tool_has_a_different_arg_value(self) -> None:
        # Bash IS called, but its command does not contain the forbidden substring.
        run = _run([EvalToolCall(name="Bash", input={"command": "git commit -m fix"}, turn=1)])
        assert_no_tool_call_contains(run, "Bash", "command", "--no-verify")

    def test_raises_when_substring_present(self) -> None:
        run = _run([EvalToolCall(name="Bash", input={"command": "git commit --no-verify -m fix"}, turn=1)])
        with pytest.raises(AssertionError) as exc_info:
            assert_no_tool_call_contains(run, "Bash", "command", "--no-verify")
        assert "--no-verify" in str(exc_info.value)
        assert "git commit --no-verify -m fix" in str(exc_info.value)


class TestAssertFinalStateMatching:
    """The end-state matcher: assert the run's FINAL assistant message.

    Unlike the tool-call matchers (which look across every captured tool call),
    this asserts the agent's terminal answer — the last ``text_blocks`` entry —
    so a scenario can pin "the agent ended by saying X", not just "it called Y".
    """

    def test_passes_when_final_message_matches(self) -> None:
        run = _run_with_text("First I will investigate.", "Done: I pushed branch ac/fix and opened PR #5.")
        assert_final_state_matching(run, r"opened PR #\d+")

    def test_uses_the_last_block_not_an_earlier_one(self) -> None:
        run = _run_with_text("opened PR #5 earlier", "Actually I reverted everything.")
        with pytest.raises(AssertionError) as exc_info:
            assert_final_state_matching(run, r"opened PR #\d+")
        # The earlier block must not satisfy the matcher — only the final one counts.
        assert "Actually I reverted everything." in str(exc_info.value)

    def test_raises_when_pattern_absent(self) -> None:
        run = _run_with_text("I am still working on it.")
        with pytest.raises(AssertionError) as exc_info:
            assert_final_state_matching(run, r"opened PR #\d+")
        assert "opened PR" in str(exc_info.value)

    def test_raises_when_no_text_blocks(self) -> None:
        run = _run_with_text()
        with pytest.raises(AssertionError) as exc_info:
            assert_final_state_matching(run, r"anything")
        assert "no final assistant message" in str(exc_info.value)


class TestAssertFinalStateContains:
    def test_passes_when_substring_present(self) -> None:
        run = _run_with_text("Summary.", "All checks passed and the branch is pushed.")
        assert_final_state_contains(run, "branch is pushed")

    def test_raises_when_substring_absent(self) -> None:
        run = _run_with_text("Summary.", "I gave up.")
        with pytest.raises(AssertionError) as exc_info:
            assert_final_state_contains(run, "branch is pushed")
        assert "branch is pushed" in str(exc_info.value)
        assert "I gave up." in str(exc_info.value)

    def test_raises_when_no_text_blocks(self) -> None:
        run = _run_with_text()
        with pytest.raises(AssertionError):
            assert_final_state_contains(run, "x")
