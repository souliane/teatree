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

[#2886](https://github.com/souliane/teatree/issues/2886) brings the
``pydantic_ai`` backend to park/resume parity with ``ClaudeSdkHarness``'s
SDK-native ``--resume <session_id>``: :class:`PydanticAiHarnessSession` can be
SEEDED with a prior ``message_history`` (constructor param, threaded through
:class:`PydanticAiHarness`), and :func:`resolve_harness` rehydrates that
history from the durable store (:mod:`teatree.agents.pydantic_ai_resume`) when
given the resuming ``Task``. The transport stays pure/injectable — persistence
lives in the sibling module, never inside the harness classes themselves.
"""

import asyncio
from collections.abc import AsyncIterator
from contextlib import AbstractAsyncContextManager, asynccontextmanager, suppress
from typing import TYPE_CHECKING, Protocol, cast

from claude_agent_sdk import AssistantMessage, ClaudeAgentOptions, ClaudeSDKClient, ResultMessage, TextBlock
from pydantic_ai import Agent
from pydantic_ai.models import Model
from pydantic_ai.models.openai import OpenAIChatModel, OpenAIChatModelSettings, ReasoningEffort
from pydantic_ai.providers.openai import OpenAIProvider

from teatree.agents.model_tiering import DEFAULT_TIER, HARNESS_EFFORT_SCALE, resolve_tier
from teatree.agents.pydantic_ai_resume import rehydrate_thread_for_resume
from teatree.config import AgentHarness, get_effective_settings
from teatree.llm.credentials import resolve_orca_router_provider_config

if TYPE_CHECKING:
    from pydantic_ai.messages import ModelMessage
    from pydantic_ai.result import StreamedRunResult

    from teatree.core.models import Task


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

    ``interrupt`` cancels the pydantic_ai ``StreamedRunResult`` (stops token
    generation, closes the underlying connection, records the interrupted state
    in message history) AND the local drain task, and sets ``_interrupted`` so
    ``receive_response`` can tell "I was deliberately interrupted" apart from an
    UNRELATED external cancellation of the awaiting coroutine itself (e.g.
    :func:`headless._drive_with_heartbeat`'s ``asyncio.wait_for`` runtime
    ceiling) — awaiting a genuine ``asyncio.Task`` propagates the awaiter's own
    cancellation straight into it, so both sources raise the identical
    ``CancelledError`` at the identical ``await task`` line; only the flag
    disambiguates them. Swallowing the latter would silently report an empty
    result instead of the runtime-breach ``stuck_reason`` the watchdog contract
    requires.

    ``history`` (#2886) SEEDS ``_history`` from a prior conversation — a
    resumed park carries the rehydrated ``list[ModelMessage]`` in here so the
    FIRST ``run_stream`` on the resumed session already includes it, matching
    ``ClaudeSDKClient``'s ``--resume`` continuation contract. The
    :attr:`history` property exposes the accumulated conversation so a caller
    (:func:`headless._collect`) can persist it back out on a subsequent park.
    """

    def __init__(
        self,
        agent: Agent[None, str],
        *,
        model_name: str,
        history: "list[ModelMessage] | None" = None,
    ) -> None:
        self._agent = agent
        self._model_name = model_name
        self._history: list[ModelMessage] = list(history) if history else []
        self._pending_prompt: str | None = None
        self._active_task: asyncio.Task[str] | None = None
        self._active_stream: StreamedRunResult[None, str] | None = None
        self._interrupted = False

    @property
    def history(self) -> "list[ModelMessage]":
        """The accumulated conversation so far (seed + every completed turn)."""
        return self._history

    async def query(self, prompt: str) -> None:
        self._pending_prompt = prompt

    async def receive_response(self) -> AsyncIterator[object]:
        if self._pending_prompt is None:
            return
        prompt, self._pending_prompt = self._pending_prompt, None
        self._interrupted = False
        async with self._agent.run_stream(prompt, message_history=self._history) as stream:
            self._active_stream = stream
            task = asyncio.ensure_future(self._drain(stream))
            self._active_task = task
            try:
                text = await task
            except asyncio.CancelledError:
                if self._interrupted:
                    return
                task.cancel()
                with suppress(asyncio.CancelledError):
                    await task
                raise
            finally:
                self._active_task = None
                self._active_stream = None
            self._history = stream.all_messages()
            run_usage = stream.usage
        yield AssistantMessage(content=[TextBlock(text=text)], model=self._model_name)
        yield ResultMessage(
            subtype="success",
            duration_ms=0,
            duration_api_ms=0,
            is_error=False,
            num_turns=1,
            session_id="",
            total_cost_usd=None,
            usage={
                "input_tokens": run_usage.input_tokens,
                "output_tokens": run_usage.output_tokens,
                "cache_read_input_tokens": run_usage.cache_read_tokens,
                "cache_creation_input_tokens": run_usage.cache_write_tokens,
            },
            result=text,
            model_usage={self._model_name: {}},
        )

    @staticmethod
    async def _drain(stream: "StreamedRunResult[None, str]") -> str:
        parts = [chunk async for chunk in stream.stream_text(delta=True)]
        return "".join(parts)

    async def interrupt(self) -> None:
        if self._active_task is None:
            return
        self._interrupted = True
        if self._active_stream is not None:
            await self._active_stream.cancel()
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

    *history* (#2886) is the rehydrated conversation of a RESUMED park, if
    any — passed straight through to the opened :class:`PydanticAiHarnessSession`
    so its first turn already carries the prior context. ``None``/absent (the
    default, and every non-resumed dispatch) opens a fresh empty conversation,
    byte-identical to before cached-resume existed.
    """

    def __init__(self, *, model: Model | None = None, history: "list[ModelMessage] | None" = None) -> None:
        self._model = model
        self._history = history

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
        # ``async with agent:`` enters the model so the provider's HTTP client
        # (OrcaRouter's OpenAI-compatible connection pool) closes cleanly on
        # exit — a bare ``Agent(...)`` never closes it, leaking a client per
        # dispatch until GC.
        async with agent:
            yield PydanticAiHarnessSession(agent, model_name=model.model_name, history=self._history)


def resolve_harness(task: "Task | None" = None) -> Harness:
    """Return the headless transport backend selected by ``agent_harness``.

    Defaults to :class:`ClaudeSdkHarness` (today's behaviour, byte-identical).
    The ``pydantic_ai`` value resolves to :class:`PydanticAiHarness`
    ([#2885](https://github.com/souliane/teatree/issues/2885)) — its OrcaRouter
    credential resolves LAZILY inside ``open``, so selecting it here never itself
    requires a live credential.

    *task* (#2886, optional — every pre-existing call site keeps working with
    none) is the task ABOUT TO DISPATCH. When the resolved backend is
    ``pydantic_ai`` and *task* is given, the resumable ancestor's persisted
    thread (:func:`~teatree.agents.pydantic_ai_resume.rehydrate_thread_for_resume`)
    is rehydrated and threaded into the constructed harness — a DB read only,
    never a network call, so this never itself requires a live credential
    either. Absent *task* (or no parked ancestor) opens a fresh conversation.
    """
    if get_effective_settings().agent_harness is AgentHarness.PYDANTIC_AI:
        history = rehydrate_thread_for_resume(task) if task is not None else []
        return PydanticAiHarness(history=history or None)
    return ClaudeSdkHarness()


def pydantic_ai_thread(session: HarnessSession) -> "list[ModelMessage] | None":
    """The session's conversation when *session* is pydantic_ai-backed, else ``None`` (#2886)."""
    return session.history if isinstance(session, PydanticAiHarnessSession) else None
