"""A real pydantic_ai session owned by a live worker for CLI subprocess tests."""

import asyncio
import importlib
import json
import os
import sys
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import django
from claude_agent_sdk import ClaudeAgentOptions
from pydantic_ai.messages import ModelMessage
from pydantic_ai.models.function import AgentInfo, DeltaToolCall, FunctionModel


class _Capture:
    tool_calls = 0
    context_tokens = None
    last_event_at = None
    open_tool = None
    result_message = None


def _read_line() -> None:
    while os.read(0, 1) not in {b"", b"\n"}:
        pass


async def _serve() -> None:
    django.setup()
    harness_type = importlib.import_module("teatree.agents.harness").PydanticAiHarness
    live_task_type = importlib.import_module("teatree.agents.live_control").LiveTask
    shared_broker = importlib.import_module("teatree.agents.live_mailbox").shared_broker
    shared_registry = importlib.import_module("teatree.agents.live_registry").shared_registry

    shared_broker()
    entered = asyncio.Event()
    release = asyncio.Event()
    requests: list[str] = []

    async def model(messages: list[ModelMessage], _info: AgentInfo) -> AsyncIterator[object]:
        requests.append(repr(messages))
        if len(requests) == 1:
            entered.set()
            await release.wait()
            yield {0: DeltaToolCall(name="Bash", json_args='{"command":"echo ok"}', tool_call_id="c1")}
        else:
            yield "done"

    harness = harness_type(model=FunctionModel(stream_function=model), phase="coding")
    task = live_task_type(pk=105, ticket=7, phase="coding", harness="pydantic_ai", model="function")
    control_dir = Path(os.environ["T3_CONTROL_DB_DIR"])

    async def serve_turn(controller: Any) -> None:
        async with harness.open(ClaudeAgentOptions()) as session:
            controller.bind(session)
            await session.query("go")

            async def consume() -> None:
                async for _message in session.receive_response():
                    pass

            running = asyncio.create_task(consume())
            await entered.wait()
            sys.stdout.write("ready\n")
            sys.stdout.flush()
            await asyncio.to_thread(_read_line)
            release.set()
            await running
            (control_dir / "requests.json").write_text(json.dumps(requests), encoding="utf-8")
            (control_dir / "turn-ended").touch()
            await asyncio.to_thread(_read_line)

    async with shared_registry().attach(task, _Capture()) as controller:
        await serve_turn(controller)
        (control_dir / "session-closed").touch()
        await asyncio.to_thread(_read_line)


if __name__ == "__main__":
    asyncio.run(_serve())
