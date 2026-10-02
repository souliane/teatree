"""Tool outcomes survive the same transcript path as tool calls."""

import json

from claude_agent_sdk import AssistantMessage, ResultMessage, ToolResultBlock, ToolUseBlock

from teatree.eval.message_mapping import eval_run_from_messages
from teatree.eval.models import EvalSpec
from teatree.eval.transcript import extract_tool_calls, parse_stream_json


def _event_stream(*blocks: dict[str, object]) -> str:
    events = [
        {"type": "assistant", "message": {"content": [blocks[0]]}},
        {"type": "user", "message": {"content": list(blocks[1:])}},
        {"type": "result", "subtype": "success", "is_error": False},
    ]
    return "\n".join(json.dumps(event) for event in events)


def test_bash_result_is_paired_by_call_id_and_carries_exit_status() -> None:
    stream = _event_stream(
        {"type": "tool_use", "id": "call-1", "name": "Bash", "input": {"command": "./run_tests"}},
        {"type": "tool_result", "tool_use_id": "call-1", "content": "exit=0\nRan 1 test\nOK", "is_error": False},
    )

    call = extract_tool_calls(parse_stream_json(stream))[0]

    assert getattr(call, "call_id", None) == "call-1"
    assert getattr(call, "exit_code", None) == 0
    assert getattr(call, "is_error", None) is False
    assert "Ran 1 test" in getattr(call, "result_excerpt", "")


def test_nonzero_shell_result_is_an_error_even_when_tool_block_says_false() -> None:
    stream = _event_stream(
        {"type": "tool_use", "id": "call-1", "name": "Bash", "input": {"command": "./run_tests"}},
        {"type": "tool_result", "tool_use_id": "call-1", "content": "exit=1\nFAILED", "is_error": False},
    )

    call = extract_tool_calls(parse_stream_json(stream))[0]

    assert getattr(call, "exit_code", None) == 1
    assert getattr(call, "is_error", None) is True


def test_missing_tool_result_stays_unverified() -> None:
    stream = json.dumps(
        {
            "type": "assistant",
            "message": {
                "content": [
                    {"type": "tool_use", "id": "call-1", "name": "Bash", "input": {"command": "./run_tests"}},
                ]
            },
        }
    )

    call = extract_tool_calls(parse_stream_json(stream))[0]

    assert getattr(call, "call_id", None) == "call-1"
    assert getattr(call, "is_error", "missing") is None
    assert getattr(call, "exit_code", "missing") is None


def test_weekly_message_mapping_and_transcript_replay_produce_the_same_outcome(tmp_path) -> None:
    spec = EvalSpec(
        name="outcome_parity",
        scenario="run a command",
        agent_path="skills/code/SKILL.md",
        prompt="run",
        matchers=(),
        source_path=tmp_path / "spec.yaml",
    )
    messages = [
        AssistantMessage(content=[ToolUseBlock(id="call-1", name="Bash", input={"command": "./run_tests"})], model="m"),
        AssistantMessage(
            content=[ToolResultBlock(tool_use_id="call-1", content="exit=0\nOK", is_error=False)], model="m"
        ),
        ResultMessage(
            subtype="success",
            duration_ms=1,
            duration_api_ms=1,
            is_error=False,
            num_turns=1,
            session_id="s",
            total_cost_usd=0.0,
            result="ok",
        ),
    ]

    live = eval_run_from_messages(spec, messages)
    replay = extract_tool_calls(parse_stream_json(live.raw_stdout))

    assert live.tool_calls == tuple(replay)
    assert getattr(live.tool_calls[0], "exit_code", None) == 0


def test_weekly_backend_retains_tool_use_and_result_event_order(tmp_path) -> None:
    spec = EvalSpec(
        name="ordered_parity",
        scenario="test before push",
        agent_path="skills/code/SKILL.md",
        prompt="run",
        matchers=(),
        source_path=tmp_path / "spec.yaml",
    )
    messages = [
        AssistantMessage(content=[ToolUseBlock(id="test", name="Bash", input={"command": "./run_tests"})], model="m"),
        AssistantMessage(content=[ToolUseBlock(id="push", name="Bash", input={"command": "git push"})], model="m"),
        AssistantMessage(
            content=[ToolResultBlock(tool_use_id="test", content="exit=0\nOK", is_error=False)], model="m"
        ),
    ]
    run = eval_run_from_messages(spec, messages)
    test_call, push_call = run.tool_calls
    assert test_call.event_index < push_call.event_index < test_call.result_event_index
