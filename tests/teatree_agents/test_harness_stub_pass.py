import asyncio
import json
from pathlib import Path

import pydantic_ai.models
import pytest
from claude_agent_sdk import ClaudeAgentOptions
from pydantic_ai.messages import ModelMessage, ModelRequest, ToolReturnPart
from pydantic_ai.models.function import AgentInfo, DeltaToolCall, FunctionModel

from teatree.agents.harness import PydanticAiHarness
from teatree.agents.lane_b import config as lane_b_config

pydantic_ai.models.ALLOW_MODEL_REQUESTS = False

_CALLS = 20
_RETURN_CHARS = 50_000
# The shell prefixes its output with this, so the command prints the rest of the 50,000.
_EXIT_PREFIX = "exit=0\n"


def _tool_output_chars(messages: list[ModelMessage]) -> int:
    return sum(
        len(str(part.content))
        for message in messages
        if isinstance(message, ModelRequest)
        for part in message.parts
        if isinstance(part, ToolReturnPart)
    )


def _big_bash_model(seen: list[int]) -> FunctionModel:
    command = f"head -c {_RETURN_CHARS - len(_EXIT_PREFIX)} /dev/zero | tr '\\0' x"

    def stream_fn(messages: list[ModelMessage], info: AgentInfo) -> object:
        seen.append(_tool_output_chars(messages))
        turn = len(seen)

        async def gen():  # noqa: RUF029 — an async generator (the stream contract) that only yields.
            if turn <= _CALLS:
                yield "Running it."
                yield {
                    0: DeltaToolCall(name="Bash", json_args=json.dumps({"command": command}), tool_call_id=f"c{turn}")
                }
            else:
                yield "done"

        return gen()

    return FunctionModel(stream_function=stream_fn)


def _run(harness: PydanticAiHarness, cwd: Path) -> None:
    async def run() -> None:
        async with harness.open(ClaudeAgentOptions(cwd=str(cwd))) as session:
            await session.query("dump it twenty times")
            async for _ in session.receive_response():
                pass

    asyncio.run(run())


class TestStubPassRunsBeforeEveryModelRequest:
    def test_a_phased_run_never_sends_more_than_the_verbatim_tail(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(lane_b_config, "_DEFAULT_SHELL_MAX_OUTPUT_BYTES", 0)
        seen: list[int] = []
        _run(PydanticAiHarness(model=_big_bash_model(seen), phase="coding"), tmp_path)

        assert len(seen) == _CALLS + 1
        assert seen[1] == _RETURN_CHARS, "the Bash output must reach the model uncapped for this measurement"
        assert max(seen) <= 6 * _RETURN_CHARS + 6 * 200
