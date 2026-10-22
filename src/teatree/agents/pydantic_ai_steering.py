"""Admit live input at a pydantic_ai request boundary."""

from collections.abc import AsyncIterator
from dataclasses import replace
from typing import NotRequired, TypedDict, Unpack

from claude_agent_sdk import ResultMessage
from pydantic_ai import Agent
from pydantic_ai.messages import ModelMessage, ModelRequest, UserPromptPart

from teatree.agents.claude_sdk_session import LiveInputQueue
from teatree.agents.live_control import RejectCode
from teatree.agents.pydantic_ai_session import PydanticAiHarnessSession
from teatree.agents.pydantic_ai_turn import SessionRun


class _SessionOptions(TypedDict):
    model_name: str
    history: NotRequired[list[ModelMessage] | None]
    phase: NotRequired[str | None]
    run: NotRequired[SessionRun | None]


class PeerInputAdmission:
    def __init__(self, inputs: LiveInputQueue) -> None:
        self._inputs = inputs

    async def __call__(self, messages: list[ModelMessage]) -> list[ModelMessage]:
        if not messages or not isinstance(messages[-1], ModelRequest):
            return messages
        text = self._inputs.take()
        if not text:
            return messages
        last = messages[-1]
        return [*messages[:-1], replace(last, parts=[*last.parts, UserPromptPart(content=text)])]


class SteerablePydanticAiSession(PydanticAiHarnessSession):
    def __init__(
        self,
        agent: Agent[None, str],
        *,
        inputs: LiveInputQueue,
        **options: Unpack[_SessionOptions],
    ) -> None:
        super().__init__(agent, **options)
        self._inputs = inputs

    async def steer(self, text: str, *, input_id: str, wait: float) -> None:
        del input_id
        await self._inputs.submit(text, wait=wait)

    async def receive_response(self) -> AsyncIterator[object]:
        self._inputs.reopen()
        try:
            async for message in super().receive_response():
                if isinstance(message, ResultMessage):
                    self._inputs.end(RejectCode.TURN_ENDED)
                yield message
        finally:
            self._inputs.end(RejectCode.TURN_ENDED)
