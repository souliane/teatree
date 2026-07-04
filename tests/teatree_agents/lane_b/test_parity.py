"""Lane parity eval — ``claude_sdk`` ↔ ``pydantic_ai`` yield the same vocabulary.

The PR-03 acceptance criteria, proven with zero tokens.

(a) A scripted ``FunctionModel`` trajectory (``ALLOW_MODEL_REQUESTS = False``)
drives the REAL :class:`PydanticAiHarness` session end-to-end, and the messages
it yields are the SAME ``claude_agent_sdk`` vocabulary the ``claude_sdk`` lane
yields — a tool call surfaces as a :class:`ToolUseBlock`, its result as a
:class:`ToolResultBlock`, the final text as a :class:`TextBlock`, closed by a
:class:`ResultMessage`.

(c) A hard-deny gate (main-clone mutation) fires on Lane B through the SAME
shared :func:`hard_deny_reason` the ``claude_sdk`` lane's PreToolUse hook
consults, surfacing an ``is_error`` :class:`ToolResultBlock`.
"""

import asyncio
import json
from pathlib import Path

import pydantic_ai.models
import pytest
from claude_agent_sdk import (
    AssistantMessage,
    ClaudeAgentOptions,
    ResultMessage,
    TextBlock,
    ToolResultBlock,
    ToolUseBlock,
)
from pydantic_ai.models.function import DeltaToolCall, FunctionModel

from teatree.agents.harness import PydanticAiHarness
from teatree.agents.lane_b.gating import hard_deny_reason

pydantic_ai.models.ALLOW_MODEL_REQUESTS = False  # ty: ignore[invalid-assignment] — the zero-token test guard.


def _streaming_model(*, tool_command: str) -> FunctionModel:
    """A streaming FunctionModel: call ``shell`` with *tool_command*, then text."""
    state = {"n": 0}

    def stream_fn(messages: object, info: object) -> object:
        state["n"] += 1
        turn = state["n"]

        async def gen():  # noqa: RUF029 — an async generator (the stream contract) that only yields.
            if turn == 1:
                args = json.dumps({"command": tool_command})
                yield {0: DeltaToolCall(name="shell", json_args=args, tool_call_id="c1")}
            else:
                yield "done"

        return gen()

    return FunctionModel(stream_function=stream_fn)


def _collect(harness: PydanticAiHarness, options: ClaudeAgentOptions, prompt: str) -> list[object]:
    async def run() -> list[object]:
        async with harness.open(options) as session:
            await session.query(prompt)
            return [message async for message in session.receive_response()]

    return asyncio.run(run())


def _blocks(messages: list[object], block_type: type) -> list[object]:
    return [
        block
        for message in messages
        if isinstance(message, AssistantMessage)
        for block in message.content
        if isinstance(block, block_type)
    ]


class TestVocabularyParity:
    def test_safe_tool_call_yields_the_sdk_message_vocabulary(self, tmp_path: Path) -> None:
        (tmp_path / "marker").write_text("")
        harness = PydanticAiHarness(model=_streaming_model(tool_command="ls"), phase="coding")
        messages = _collect(harness, ClaudeAgentOptions(cwd=str(tmp_path)), "list the dir")

        # Every yielded message is a claude_agent_sdk type — the seam's contract.
        assert all(isinstance(m, (AssistantMessage, ResultMessage)) for m in messages)
        # The tool call + its result surfaced in the seam's tool-block vocabulary.
        tool_uses = _blocks(messages, ToolUseBlock)
        assert [t.name for t in tool_uses] == ["shell"]
        tool_results = _blocks(messages, ToolResultBlock)
        assert tool_results
        assert any("marker" in str(r.content) for r in tool_results)
        # The final text + a terminal ResultMessage.
        assert [b.text for b in _blocks(messages, TextBlock)] == ["done"]
        assert isinstance(messages[-1], ResultMessage)


class TestHardDenyParity:
    def test_main_clone_mutation_is_refused_on_lane_b(self, tmp_path: Path) -> None:
        command = "git reset --hard HEAD~1"
        # The shared evaluator the claude_sdk lane's PreToolUse hook also consults.
        assert hard_deny_reason("shell", {"command": command}) is not None

        harness = PydanticAiHarness(model=_streaming_model(tool_command=command), phase="coding")
        messages = _collect(harness, ClaudeAgentOptions(cwd=str(tmp_path)), "reset hard")

        error_results = [r for r in _blocks(messages, ToolResultBlock) if r.is_error]
        assert error_results, "a refused tool call must surface an is_error ToolResultBlock"
        assert any("BLOCKED" in str(r.content) for r in error_results)


def test_zero_tokens_enforced() -> None:
    assert pydantic_ai.models.ALLOW_MODEL_REQUESTS is False


if __name__ == "__main__":  # pragma: no cover
    pytest.main([__file__, "-q"])
