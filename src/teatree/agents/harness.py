"""The provider-agnostic harness seam for the headless agent runtime.

The headless runner (:mod:`teatree.agents.headless`) drives an in-process agent
session behind a narrow protocol pair — :class:`Harness` opens a session for a
built set of options, :class:`HarnessSession` is the in-flight session surface the
driver talks to. :func:`resolve_harness` reads the DB-home ``agent_harness``
setting and returns the backend.

PR-1 (#2565) ships one backend, :class:`ClaudeSdkHarness`, wrapping today's
``claude-agent-sdk`` ``ClaudeSDKClient`` — the default, so the transport is
byte-identical to before the seam existed. A future provider-agnostic backend
(``agent_harness = pydantic_ai``) lands behind the same protocol; its enum value
is reserved and :func:`resolve_harness` refuses it with a clear
``NotImplementedError`` until then, mirroring the ``AgentRuntime.API`` precedent.
"""

from collections.abc import AsyncIterator
from contextlib import AbstractAsyncContextManager, asynccontextmanager
from typing import Protocol

from claude_agent_sdk import ClaudeAgentOptions, ClaudeSDKClient

from teatree.config import AgentHarness, get_effective_settings


class HarnessSession(Protocol):
    """The in-flight session surface the driver uses.

    Method names match ``claude_agent_sdk.ClaudeSDKClient`` exactly (``query`` /
    ``receive_response`` / ``interrupt``) so the real client satisfies the
    protocol structurally, with no adapter.
    """

    async def query(self, prompt: str) -> None: ...

    def receive_response(self) -> AsyncIterator[object]: ...

    async def interrupt(self) -> None: ...


class Harness(Protocol):
    """Opens a :class:`HarnessSession` for a built set of agent options."""

    def open(self, options: ClaudeAgentOptions) -> AbstractAsyncContextManager[HarnessSession]: ...


class ClaudeSdkHarness:
    """The default backend — the ``claude-agent-sdk`` in-process transport."""

    @staticmethod
    @asynccontextmanager
    async def open(options: ClaudeAgentOptions) -> AsyncIterator[HarnessSession]:
        async with ClaudeSDKClient(options=options) as client:
            yield client


def resolve_harness() -> Harness:
    """Return the headless transport backend selected by ``agent_harness``.

    Defaults to :class:`ClaudeSdkHarness`. The reserved ``pydantic_ai`` value
    raises ``NotImplementedError`` — the provider-agnostic backend lands in a
    later PR (the ``AgentRuntime.API`` precedent).
    """
    if get_effective_settings().agent_harness is AgentHarness.PYDANTIC_AI:
        msg = (
            "agent_harness=pydantic_ai (provider-agnostic OpenAI-compatible transport) "
            "is not implemented yet; use claude_sdk"
        )
        raise NotImplementedError(msg)
    return ClaudeSdkHarness()
