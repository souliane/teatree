import pytest
from pydantic_ai.messages import ToolCallPart, ToolReturnPart

from teatree.agents.pydantic_ai_turn import ToolCallRecord


@pytest.mark.parametrize("args", [None, {}, ""])
def test_empty_tool_call_argument_bytes_match_provider_json(args: dict[str, str] | str | None) -> None:
    part = ToolCallPart(tool_name="Read", args=args, tool_call_id="call-1")

    record = ToolCallRecord.start(part)

    assert record.args_bytes == len(part.args_as_json_str().encode())


@pytest.mark.parametrize("outcome", ["success", "failed", "retried", "denied", "interrupted"])
def test_tool_return_bytes_match_provider_response(outcome: str) -> None:
    call = ToolCallPart(tool_name="Read", args={}, tool_call_id="call-1")
    returned = ToolReturnPart(tool_name="Read", content="boom", tool_call_id="call-1", outcome=outcome)
    record = ToolCallRecord.start(call)

    record.finish(returned)

    assert record.output_bytes == len(returned.model_response_str().encode())
