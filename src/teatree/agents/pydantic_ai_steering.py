"""Steer a live pydantic_ai run through its native enqueue API."""

import asyncio
from collections.abc import AsyncIterator
from typing import TYPE_CHECKING, NotRequired, TypedDict, Unpack

from claude_agent_sdk import ResultMessage
from pydantic_ai import Agent
from pydantic_ai.exceptions import UserError
from pydantic_ai.messages import EnqueuedMessagesEvent, ModelMessage

from teatree.agents.live_control import RejectCode, SteerRejectedError
from teatree.agents.pydantic_ai_session import PydanticAiHarnessSession
from teatree.agents.pydantic_ai_turn import SessionRun

if TYPE_CHECKING:
    from pydantic_ai.messages import AgentStreamEvent


class _SessionOptions(TypedDict):
    model_name: str
    history: NotRequired[list[ModelMessage] | None]
    phase: NotRequired[str | None]
    run: NotRequired[SessionRun | None]


class SteerablePydanticAiSession(PydanticAiHarnessSession):
    def __init__(
        self,
        agent: Agent[None, str],
        **options: Unpack[_SessionOptions],
    ) -> None:
        super().__init__(agent, **options)
        self._waiters: dict[str, asyncio.Future[None]] = {}
        self._closed = False
        self._turn_ended = False
        self._input_observer = self._observe_input

    async def steer(self, text: str, *, input_id: str, wait: float) -> None:
        del input_id
        if self._closed:
            raise SteerRejectedError(RejectCode.SESSION_CLOSED)
        run = self._active_run
        if run is None:
            raise SteerRejectedError(RejectCode.TURN_ENDED if self._turn_ended else RejectCode.NOT_STEERABLE)
        try:
            enqueue_id = run.enqueue(text, priority="asap")
        except UserError as exc:
            raise SteerRejectedError(RejectCode.TURN_ENDED) from exc
        if enqueue_id is None:
            raise SteerRejectedError(RejectCode.NOT_STEERABLE)
        accepted = asyncio.get_running_loop().create_future()
        self._waiters[enqueue_id] = accepted
        try:
            await asyncio.wait_for(asyncio.shield(accepted), timeout=wait)
        finally:
            self._waiters.pop(enqueue_id, None)
            if not accepted.done():
                accepted.cancel()

    async def follow_up(self, prompt: str) -> None:
        if self._closed:
            raise SteerRejectedError(RejectCode.SESSION_CLOSED)
        if not self._turn_ended or self._active_run is not None:
            raise SteerRejectedError(RejectCode.NOT_IDLE)
        await self.query(prompt)
        self._turn_ended = False

    def _observe_input(self, event: "AgentStreamEvent") -> None:
        if isinstance(event, EnqueuedMessagesEvent):
            accepted = self._waiters.get(event.enqueue_id)
            if accepted is not None and not accepted.done():
                accepted.set_result(None)

    def close(self) -> None:
        self._closed = True
        self._end_turn(RejectCode.SESSION_CLOSED)

    def _end_turn(self, code: RejectCode) -> None:
        for accepted in self._waiters.values():
            if not accepted.done():
                accepted.set_exception(SteerRejectedError(code))

    async def receive_response(self) -> AsyncIterator[object]:
        self._turn_ended = False
        try:
            async for message in super().receive_response():
                if isinstance(message, ResultMessage):
                    self._turn_ended = True
                yield message
        finally:
            self._turn_ended = True
            self._end_turn(RejectCode.TURN_ENDED)
