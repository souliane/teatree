"""The ``claude_sdk`` session: the SDK client plus operator input delivered at TeaTree-owned hook boundaries.

Input reaches the CURRENT turn only — through a main-thread ``PostToolUse`` ``additionalContext``
or a ``Stop`` block — so steering can never start a turn of its own (CLI 2.1.284 contract,
``tests/fixtures/claude_cli/2.1.284-live-control.json``).
"""

import asyncio
from collections import deque
from collections.abc import AsyncIterator, Mapping
from contextlib import suppress
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, cast

from claude_agent_sdk import HookMatcher, ResultMessage
from claude_agent_sdk.types import (
    HookCallback,
    HookContext,
    HookEvent,
    HookJSONOutput,
    PostToolUseHookInput,
    StopHookInput,
)

from teatree.agents.live_control import RejectCode, SteerRejectedError

if TYPE_CHECKING:
    from teatree.agents.harness import HarnessSession


@dataclass(slots=True)
class _PendingInput:
    text: str
    accepted: asyncio.Future[None]


class LiveInputQueue:
    """Operator inputs waiting for the running turn's next safe boundary.

    An input is delivered once or withdrawn: a consumer only takes inputs whose future is still
    open, and withdrawal settles the future, both on the session's own loop.
    """

    def __init__(self) -> None:
        self._pending: deque[_PendingInput] = deque()
        self._ended: RejectCode | None = None

    def hooks_merged_into(
        self, hooks: Mapping[HookEvent, list[HookMatcher]] | None
    ) -> dict[HookEvent, list[HookMatcher]]:
        merged = dict(hooks or {})
        for event, callback in (("PostToolUse", self._post_tool_use), ("Stop", self._stop)):
            merged[event] = [*merged.get(event, []), HookMatcher(hooks=[cast("HookCallback", callback)])]
        return merged

    async def submit(self, text: str, *, wait: float) -> None:
        if self._ended is not None:
            raise SteerRejectedError(self._ended)
        item = _PendingInput(text, asyncio.get_running_loop().create_future())
        self._pending.append(item)
        try:
            await asyncio.wait({item.accepted}, timeout=wait)
        finally:
            if not item.accepted.done():
                item.accepted.cancel()
            with suppress(ValueError):
                self._pending.remove(item)
        if item.accepted.cancelled():
            raise SteerRejectedError(RejectCode.NOT_ACCEPTED_IN_TIME)
        item.accepted.result()

    def end(self, code: RejectCode) -> None:
        """Refuse every input still waiting, and every later one, with *code*."""
        if self._ended is None:
            self._ended = code
        while self._pending:
            item = self._pending.popleft()
            if not item.accepted.done():
                item.accepted.set_exception(SteerRejectedError(code))

    def _take(self) -> str:
        texts = []
        while self._pending:
            item = self._pending.popleft()
            if not item.accepted.done():
                item.accepted.set_result(None)
                texts.append(item.text)
        return "\n\n".join(texts)

    async def _post_tool_use(
        self, input_data: PostToolUseHookInput, tool_use_id: str | None, context: HookContext
    ) -> HookJSONOutput:
        del tool_use_id, context
        if input_data.get("agent_id"):
            return {}
        text = self._take()
        return {"hookSpecificOutput": {"hookEventName": "PostToolUse", "additionalContext": text}} if text else {}

    async def _stop(self, input_data: StopHookInput, tool_use_id: str | None, context: HookContext) -> HookJSONOutput:
        del input_data, tool_use_id, context
        text = self._take()
        return {"decision": "block", "reason": text} if text else {}


class ClaudeSdkSession:
    """The SDK client as a :class:`~teatree.agents.live_control.SteerableSession`."""

    def __init__(self, client: "HarnessSession", inputs: LiveInputQueue) -> None:
        self._client = client
        self._inputs = inputs

    async def query(self, prompt: str) -> None:
        await self._client.query(prompt)

    async def receive_response(self) -> AsyncIterator[Any]:
        async for message in self._client.receive_response():
            if isinstance(message, ResultMessage):
                self._inputs.end(RejectCode.TURN_ENDED)
            yield message

    async def interrupt(self) -> None:
        await self._client.interrupt()

    async def steer(self, text: str, *, input_id: str, wait: float) -> None:
        del input_id
        await self._inputs.submit(text, wait=wait)
