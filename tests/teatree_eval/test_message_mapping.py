"""The SDK-message mapper folds hook events into `EvalRun.gate_events`."""

import dataclasses
from pathlib import Path

from claude_agent_sdk import AssistantMessage, ResultMessage, TextBlock, ThinkingBlock, ToolResultBlock, ToolUseBlock
from claude_agent_sdk.types import HookEventMessage

from teatree.eval import message_mapping
from teatree.eval.message_mapping import _block_to_dict, eval_run_from_messages
from teatree.eval.models import EvalSpec, Matcher, PlanBeforeToolMatcher
from teatree.eval.report import evaluate, render_html, render_text
from teatree.eval.transcript import (
    StreamJsonEvent,
    extract_terminal_reason,
    extract_text_blocks,
    extract_tool_calls,
    parse_stream_json,
)


def test_module_docstring_names_the_live_from_obj_seam() -> None:
    """The docstring's fold-to-events cross-ref must name the classmethod that exists.

    The typed lane folds each event dict straight into ``StreamJsonEvent`` via its
    ``from_obj`` classmethod; an earlier draft named a free ``event_from_obj`` function that
    never existed, so the ``:meth:`` role dangled. Guard the reference against regrowth.
    """
    doc = message_mapping.__doc__ or ""
    assert "StreamJsonEvent.from_obj" in doc
    assert "event_from_obj" not in doc
    assert callable(StreamJsonEvent.from_obj)


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


def test_hook_response_preserves_pretool_audit_context() -> None:
    messages = [
        HookEventMessage(
            subtype="hook_response",
            hook_event_name="PreToolUse",
            data={
                "hook_event": "PreToolUse",
                "outcome": "allow",
                "output": '{"permissionDecision":"allow"}',
                "sequence": 2,
                "tool_name": "Bash",
                "tool_use_id": "call-2",
                "gate_id": "visible_plan_gate",
                "assistant_text": "Plan — PROJ-4521: implement, test, verify.",
            },
        ),
        _result(),
    ]

    event = eval_run_from_messages(_spec(), messages).gate_events[0]
    assert event.sequence == 2
    assert event.tool_name == "Bash"
    assert event.tool_use_id == "call-2"
    assert event.gate_id == "visible_plan_gate"
    assert event.assistant_text == "Plan — PROJ-4521: implement, test, verify."


def test_sdk_hook_response_is_enriched_from_the_governed_tool_turn() -> None:
    spec = dataclasses.replace(
        _spec(),
        matchers=(PlanBeforeToolMatcher(governed_tools=("Bash",), patterns=(r"PROJ-4521",)),),
    )
    messages = [
        AssistantMessage(
            content=[
                TextBlock(text="Plan — PROJ-4521: inspect, implement, and verify."),
                ToolUseBlock(id="t1", name="Bash", input={"command": "echo hi"}),
            ],
            model="haiku",
        ),
        HookEventMessage(
            subtype="hook_response",
            hook_event_name="PreToolUse",
            data={"hook_event": "PreToolUse", "outcome": "allow", "output": "allowed"},
        ),
        _result(),
    ]

    result = evaluate(spec, eval_run_from_messages(spec, messages))

    assert result.passed


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


class TestTerminalErrorTextReachesTheReport:
    """A provider/run error's own message survives into the run and the rendered report.

    The non-CLI lanes (`pydantic_ai` / `anthropic_api`) report a failed run as an
    error-shaped terminal message carrying `str(exc)` and yield no tool blocks and no
    text, so the trajectory is empty. Dropping that message left `error_during_execution`
    with an empty transcript and the cause nowhere in any lane artifact.
    """

    @staticmethod
    def _error_result(message: str) -> ResultMessage:
        return ResultMessage(
            subtype="error_during_execution",
            duration_ms=0,
            duration_api_ms=0,
            is_error=True,
            num_turns=3,
            session_id="s",
            total_cost_usd=0.0,
            result=message,
        )

    def test_error_message_lands_on_raw_stderr(self) -> None:
        run = eval_run_from_messages(_spec(), [self._error_result("Exceeded maximum retries (2)")])

        assert run.terminal_reason == "error_during_execution"
        assert run.is_error
        assert run.raw_stderr == "Exceeded maximum retries (2)"

    def test_a_clean_run_carries_no_stderr(self) -> None:
        run = eval_run_from_messages(_spec(), [_result()])

        assert run.raw_stderr == ""

    def test_text_report_shows_the_cause_beside_the_failed_matcher(self) -> None:
        spec = _spec()
        results = [evaluate(spec, eval_run_from_messages(spec, [self._error_result("boom: the provider refused")]))]

        rendered = render_text(results)

        assert "run errored: error_during_execution" in rendered
        assert "boom: the provider refused" in rendered

    def test_html_report_shows_the_cause_beside_the_failed_matcher(self) -> None:
        spec = _spec()
        results = [evaluate(spec, eval_run_from_messages(spec, [self._error_result("boom: the provider refused")]))]

        rendered = render_html(results)

        assert "run errored:" in rendered
        assert "boom: the provider refused" in rendered
