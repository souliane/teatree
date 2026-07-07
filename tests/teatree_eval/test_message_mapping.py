"""The SDK-message mapper folds hook events into `EvalRun.gate_events`."""

from pathlib import Path

from claude_agent_sdk import AssistantMessage, ResultMessage, TextBlock, ToolUseBlock
from claude_agent_sdk.types import HookEventMessage

from teatree.eval.message_mapping import eval_run_from_messages
from teatree.eval.models import EvalSpec, Matcher


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
