"""The SDK-message mapper folds hook events into `EvalRun.gate_events`."""

from pathlib import Path

from claude_agent_sdk import AssistantMessage, ResultMessage, TextBlock, ThinkingBlock, ToolResultBlock, ToolUseBlock
from claude_agent_sdk.types import HookEventMessage

from teatree.eval.message_mapping import _block_to_dict, eval_run_from_messages
from teatree.eval.models import EvalSpec, Matcher
from teatree.eval.report import evaluate
from teatree.eval.transcript import extract_terminal_reason, extract_text_blocks, extract_tool_calls, parse_stream_json


def _spec() -> EvalSpec:
    return EvalSpec(
        name="hooked",
        scenario="a hooked scenario",
        agent_path="skills/rules/SKILL.md",
        prompt="do the thing",
        matchers=(Matcher(kind="positive", tool="Bash", arg_path="command", operator="~", value="."),),
        source_path=Path("spec.yaml"),
        model="claude-haiku-4-5",
    )


def _result() -> ResultMessage:
    return ResultMessage(
        subtype="success",
        duration_ms=1,
        duration_api_ms=1,
        is_error=False,
        num_turns=1,
        session_id="s",
        total_cost_usd=0.0,
        result="ok",
    )


def test_hook_response_stop_block_becomes_a_gate_event() -> None:
    messages = [
        AssistantMessage(content=[ToolUseBlock(id="t1", name="Bash", input={"command": "echo hi"})], model="haiku"),
        HookEventMessage(
            subtype="hook_response",
            hook_event_name="Stop",
            data={"hook_event": "Stop", "outcome": "block", "output": "decision: block"},
        ),
        _result(),
    ]
    run = eval_run_from_messages(_spec(), messages)
    assert any(event.is_stop_block for event in run.gate_events)
    # The hook event never leaks into the tool-call stream the grader reads.
    assert [c.name for c in run.tool_calls] == ["Bash"]


def test_hook_started_is_dropped_and_not_a_gate_event() -> None:
    messages = [
        HookEventMessage(subtype="hook_started", hook_event_name="Stop", data={"hook_event": "Stop"}),
        _result(),
    ]
    run = eval_run_from_messages(_spec(), messages)
    assert run.gate_events == ()


def test_no_hook_messages_yields_empty_gate_events() -> None:
    messages = [
        AssistantMessage(content=[TextBlock(text="hi")], model="haiku"),
        _result(),
    ]
    run = eval_run_from_messages(_spec(), messages)
    assert run.gate_events == ()


class TestBlockRendering:
    """`_block_to_dict` renders every SDK block to its canonical shape, never `unknown`.

    The pydantic_ai lane surfaces tool results and gate refusals as
    ``ToolResultBlock`` (harness ``_tool_blocks_since``) and a reasoning model emits
    ``ThinkingBlock``; the mapper used to collapse both to ``{"type": "unknown"}``.
    """

    def test_tool_result_block_renders_as_tool_result(self) -> None:
        rendered = _block_to_dict(ToolResultBlock(tool_use_id="t1", content="ran ok", is_error=False))
        assert rendered == {"type": "tool_result", "tool_use_id": "t1", "content": "ran ok", "is_error": False}

    def test_thinking_block_renders_as_thinking(self) -> None:
        rendered = _block_to_dict(ThinkingBlock(thinking="reasoning", signature="sig"))
        assert rendered == {"type": "thinking", "thinking": "reasoning", "signature": "sig"}

    def test_tool_result_in_the_stream_is_graded_not_dropped_to_unknown(self) -> None:
        # A run interleaving a tool call, its result, and the final text still grades
        # the tool call — the tool_result block no longer becomes an opaque `unknown`.
        messages = [
            AssistantMessage(content=[ToolUseBlock(id="t1", name="Bash", input={"command": "echo hi"})], model="m"),
            AssistantMessage(content=[ToolResultBlock(tool_use_id="t1", content="hi", is_error=False)], model="m"),
            AssistantMessage(content=[TextBlock(text="done")], model="m"),
            _result(),
        ]
        run = eval_run_from_messages(_spec(), messages)
        assert [c.name for c in run.tool_calls] == ["Bash"]
        assert '"type": "tool_result"' in run.raw_stdout
        assert '"type": "unknown"' not in run.raw_stdout


def test_direct_fold_matches_a_reparse_of_the_synthesized_stream() -> None:
    # The mapper folds the event dicts DIRECTLY into StreamJsonEvents (no JSON
    # string round-trip). This pins that the direct fold is equivalent to re-parsing
    # the synthesized `raw_stdout` through the transcript extractors, so the two
    # folding paths can never silently diverge.
    messages = [
        AssistantMessage(content=[ToolUseBlock(id="t1", name="Bash", input={"command": "git status"})], model="m"),
        AssistantMessage(content=[TextBlock(text="all clean")], model="m"),
        _result(),
    ]
    run = eval_run_from_messages(_spec(), messages)
    reparsed = parse_stream_json(run.raw_stdout)
    assert [c.name for c in run.tool_calls] == [c.name for c in extract_tool_calls(reparsed)]
    assert list(run.text_blocks) == extract_text_blocks(reparsed)
    assert (run.terminal_reason, run.is_error) == extract_terminal_reason(reparsed)


def test_no_messages_yields_an_empty_shaped_run() -> None:
    run = eval_run_from_messages(_spec(), [])
    assert run.raw_stdout == ""
    assert run.tool_calls == ()
    assert run.text_blocks == ()


def test_a_captured_run_grades_green() -> None:
    # End-to-end sanity: a captured tool call satisfying the spec's matcher passes.
    messages = [
        AssistantMessage(content=[ToolUseBlock(id="t1", name="Bash", input={"command": "run the tests"})], model="m"),
        AssistantMessage(content=[TextBlock(text="done")], model="m"),
        _result(),
    ]
    result = evaluate(_spec(), eval_run_from_messages(_spec(), messages))
    assert result.passed
