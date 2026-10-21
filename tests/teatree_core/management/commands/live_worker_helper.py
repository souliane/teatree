"""A stand-in worker for the ``live`` CLI tests: the real broker and registry, scripted sessions.

Run as ``python live_worker_helper.py`` with ``T3_CONTROL_DB_DIR`` set; it publishes its ingress
socket under ``$T3_CONTROL_DB_DIR/live``, prints ``ready`` and serves until stdin closes. The
sessions are the doubles: task 101 accepts, 102 refuses ``turn_ended``, 103 kills this process
mid-request (the client sees a lost connection), 104 is not steerable.
"""

import asyncio
import contextlib
import os
import sys
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from pathlib import Path
from time import monotonic
from typing import Any
from unittest.mock import AsyncMock

from claude_agent_sdk import ClaudeAgentOptions

from teatree.agents.codex_app_server import CodexAppServerSession
from teatree.agents.codex_app_server_options import CodexAppServerOptions
from teatree.agents.live_control import LiveTask, RejectCode, SteerRejectedError
from teatree.agents.live_mailbox import shared_broker
from teatree.agents.live_registry import shared_registry


class _Session:
    async def query(self, prompt: str) -> None: ...

    async def receive_response(self) -> AsyncIterator[object]:
        return
        yield

    async def interrupt(self) -> None: ...


class _ScriptedSession(_Session):
    def __init__(self, behaviour: str) -> None:
        self.behaviour = behaviour

    async def steer(self, text: str, *, input_id: str, wait: float) -> None:
        if self.behaviour == "turn_ended":
            raise SteerRejectedError(RejectCode.TURN_ENDED)
        if self.behaviour == "die":
            os._exit(0)


def _codex_session() -> CodexAppServerSession:
    options = CodexAppServerOptions.from_sdk_options(ClaudeAgentOptions(cwd="/tmp", mcp_servers={}))
    session = CodexAppServerSession(options, resume=None, code_home=Path("/tmp"))
    session.thread_id = "thread-105"
    session._turn_opened("turn-105")

    async def request(method: str, params: dict[str, Any]) -> dict[str, Any]:
        await asyncio.sleep(0)
        return {"turnId": "turn-105"}

    session._request = AsyncMock(side_effect=request)
    return session


@dataclass
class _BusyStream:
    """What a driver's stream capture holds while its agent sits in one Bash call."""

    tool_calls: int = 1
    context_tokens: int | None = None
    last_event_at: float | None = field(default_factory=monotonic)
    open_tool: tuple[str, float] | None = field(default_factory=lambda: ("Bash", monotonic()))
    result_message: object = None


async def _serve() -> None:
    shared_broker()
    sessions: list[tuple[int, _Session | CodexAppServerSession]] = [
        (101, _ScriptedSession("accept")),
        (102, _ScriptedSession("turn_ended")),
        (103, _ScriptedSession("die")),
        (104, _Session()),
        (105, _codex_session()),
    ]
    async with contextlib.AsyncExitStack() as stack:
        for pk, session in sessions:
            codex = pk == 105
            task = LiveTask(
                pk=pk,
                ticket=7,
                phase="coding",
                harness="codex_app_server" if codex else "claude_sdk",
                model="codex-test" if codex else "claude-test",
            )
            controller = await stack.enter_async_context(shared_registry().attach(task, _BusyStream()))
            controller.bind(session)
        sys.stdout.write("ready\n")
        sys.stdout.flush()
        await asyncio.to_thread(sys.stdin.read)


if __name__ == "__main__":
    asyncio.run(_serve())
