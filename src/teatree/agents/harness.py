"""The provider-agnostic harness seam for the headless agent runtime.

The headless runner (:mod:`teatree.agents.headless`) drives an in-process agent
session behind a narrow protocol pair — :class:`Harness` opens a session for a
built set of options, :class:`HarnessSession` is the in-flight session surface the
driver talks to. :func:`resolve_harness` reads the DB-home ``agent_harness``
setting and returns the backend.

PR-1 (#2565, #2883) shipped :class:`ClaudeSdkHarness`, wrapping today's
``claude-agent-sdk`` ``ClaudeSDKClient`` — the default, so the transport is
byte-identical to before the seam existed. PR-2
([#2885](https://github.com/souliane/teatree/issues/2885)) ships the
provider-agnostic backend, :class:`PydanticAiHarness`: a Pydantic AI
:class:`~pydantic_ai.Agent` targeting OrcaRouter's OpenAI-compatible, BYOK,
metered endpoint. Both backends yield the SAME ``claude_agent_sdk`` message
vocabulary (``AssistantMessage`` / ``ResultMessage``) from :meth:`HarnessSession.receive_response`
so the driver (:func:`teatree.agents.headless._collect`) never special-cases the
transport — that vocabulary IS the seam's provider-agnostic contract, proved by
the ``FakeHarnessSession`` test double yielding the identical shape.
"""

import asyncio
from collections.abc import AsyncIterator
from contextlib import AbstractAsyncContextManager, asynccontextmanager
from typing import TYPE_CHECKING, Protocol, cast

from claude_agent_sdk import AssistantMessage, ClaudeAgentOptions, ClaudeSDKClient, ResultMessage, TextBlock
from pydantic_ai import Agent
from pydantic_ai.models import Model
from pydantic_ai.models.openai import OpenAIChatModel, OpenAIChatModelSettings, ReasoningEffort
from pydantic_ai.providers.openai import OpenAIProvider

from teatree.agents.model_tiering import DEFAULT_TIER, HARNESS_EFFORT_SCALE, resolve_tier
from teatree.config import AgentHarness, get_effective_settings
from teatree.llm.credentials import resolve_orca_router_provider_config

if TYPE_CHECKING:
    from pydantic_ai.messages import ModelMessage
    from pydantic_ai.result import StreamedRunResult


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


def _extract_system_prompt(options: ClaudeAgentOptions) -> str:
    """Pull the custom system context out of *options* for the pydantic_ai Agent.

    ``ClaudeAgentOptions.system_prompt`` is normally a ``SystemPromptPreset``
    (``{"type": "preset", "preset": "claude_code", "append": <context>}``) —
    the ``claude_code`` preset itself is meaningless outside the bundled CLI, so
    only the appended custom context is portable; a plain ``str`` (as tests build)
    is used as-is; anything else (a ``SystemPromptFile`` reference, or ``None``)
    has no portable content here.
    """
    prompt = options.system_prompt
    if isinstance(prompt, str):
        return prompt
    if isinstance(prompt, dict) and prompt.get("type") == "preset":
        return str(prompt.get("append", ""))
    return ""


def _resolve_effort(options: ClaudeAgentOptions) -> ReasoningEffort | None:
    """Map ``options.effort`` onto pydantic_ai's ``ReasoningEffort`` vocabulary.

    ``options.effort`` is already scoped to the ACTIVE harness by
    :func:`teatree.agents.model_tiering.resolve_spawn_effort` (called while
    *options* was built), so this is normally a pass-through; the
    :data:`~teatree.agents.model_tiering.HARNESS_EFFORT_SCALE` re-check is a
    defence-in-depth guard for options built outside that resolver (e.g. a test),
    dropping an out-of-vocabulary value (``max``, the one rung
    ``claude_sdk`` has that ``pydantic_ai`` does not) rather than handing the SDK
    a reasoning-effort string it will reject.
    """
    effort = options.effort
    if effort is None or effort not in HARNESS_EFFORT_SCALE[AgentHarness.PYDANTIC_AI]:
        return None
    return cast("ReasoningEffort", effort)


class PydanticAiHarnessSession:
    """The ``pydantic_ai`` in-flight session — the ``HarnessSession`` surface over an ``Agent``.

    Adapts pydantic_ai's streamed output into the SAME ``claude_agent_sdk``
    message vocabulary every backend yields (module docstring), so the driver
    never special-cases the transport. ``query``/``receive_response`` are
    decoupled (one queued prompt consumed per turn) so a multi-turn conversation
    keeps ``message_history`` across calls, matching ``ClaudeSDKClient``'s
    contract — proved by :mod:`tests.teatree_agents.test_harness`.

    ``interrupt`` cancels the ASYNCIO TASK draining the in-flight stream (not
    pydantic_ai's own ``StreamedRunResult.cancel``) — incremental draining via
    ``stream_text(delta=True)`` yields control at every chunk, which is a genuine
    ``asyncio`` cancellation point a concurrent ``task.cancel()`` reliably
    interrupts; a single blocking ``await stream.get_output()`` is not.
    """

    def __init__(self, agent: Agent[None, str], *, model_name: str) -> None:
        self._agent = agent
        self._model_name = model_name
        self._history: list[ModelMessage] = []
        self._pending_prompt: str | None = None
        self._active_task: asyncio.Task[str] | None = None

    async def query(self, prompt: str) -> None:
        self._pending_prompt = prompt

    async def receive_response(self) -> AsyncIterator[object]:
        if self._pending_prompt is None:
            return
        prompt, self._pending_prompt = self._pending_prompt, None
        async with self._agent.run_stream(prompt, message_history=self._history) as stream:
            task = asyncio.ensure_future(self._drain(stream))
            self._active_task = task
            try:
                text = await task
            except asyncio.CancelledError:
                return
            finally:
                self._active_task = None
            self._history = stream.all_messages()
        yield AssistantMessage(content=[TextBlock(text=text)], model=self._model_name)
        yield ResultMessage(
            subtype="success",
            duration_ms=0,
            duration_api_ms=0,
            is_error=False,
            num_turns=1,
            session_id="",
            total_cost_usd=None,
            usage=None,
            result=text,
            model_usage=None,
        )

    @staticmethod
    async def _drain(stream: "StreamedRunResult[None, str]") -> str:
        parts = [chunk async for chunk in stream.stream_text(delta=True)]
        return "".join(parts)

    async def interrupt(self) -> None:
        if self._active_task is not None:
            self._active_task.cancel()


class PydanticAiHarness:
    """The ``pydantic_ai`` backend — OrcaRouter BYOK, OpenAI-compatible transport.

    ``open`` builds a fresh :class:`~pydantic_ai.Agent` from *options* (model,
    system prompt, and reasoning effort — MCP servers, hooks, and tool
    permissions are the ``claude-agent-sdk``-specific surface the strangler-fig
    migration re-homes in a later PR, per the redesign doc's port-surface table)
    targeting OrcaRouter's OpenAI-compatible endpoint with the BYOK metered
    credential (:func:`~teatree.llm.credentials.resolve_orca_router_provider_config`).

    *model* is INJECTABLE (default ``None`` triggers the real OrcaRouter
    resolution lazily, INSIDE ``open`` — never at construction time, so building
    the harness never requires a live credential) so tests drive it with
    pydantic_ai's own :class:`~pydantic_ai.models.test.TestModel` /
    :class:`~pydantic_ai.models.function.FunctionModel` doubles, with no network
    and no :class:`~teatree.llm.credentials.CredentialError` risk.
    """

    def __init__(self, *, model: Model | None = None) -> None:
        self._model = model

    def _resolve_model(self, options: ClaudeAgentOptions) -> Model:
        if self._model is not None:
            return self._model
        config = resolve_orca_router_provider_config()
        provider = OpenAIProvider(base_url=config.base_url, api_key=config.api_key)
        model_name = options.model or resolve_tier(DEFAULT_TIER)
        return OpenAIChatModel(model_name, provider=provider)

    @asynccontextmanager
    async def open(self, options: ClaudeAgentOptions) -> AsyncIterator[HarnessSession]:
        model = self._resolve_model(options)
        effort = _resolve_effort(options)
        model_settings = OpenAIChatModelSettings(openai_reasoning_effort=effort) if effort else None
        agent = Agent(model, system_prompt=_extract_system_prompt(options), model_settings=model_settings)
        yield PydanticAiHarnessSession(agent, model_name=model.model_name)


def resolve_harness() -> Harness:
    """Return the headless transport backend selected by ``agent_harness``.

    Defaults to :class:`ClaudeSdkHarness` (today's behaviour, byte-identical).
    The ``pydantic_ai`` value resolves to :class:`PydanticAiHarness`
    ([#2885](https://github.com/souliane/teatree/issues/2885)) — its OrcaRouter
    credential resolves LAZILY inside ``open``, so selecting it here never itself
    requires a live credential.
    """
    if get_effective_settings().agent_harness is AgentHarness.PYDANTIC_AI:
        return PydanticAiHarness()
    return ClaudeSdkHarness()
