import pytest

from teatree.eval.matchers import assert_no_tool_call_matching, assert_tool_call_contains
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
