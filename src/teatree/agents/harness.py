"""The provider-agnostic harness seam for the headless agent runtime.

The agent runner (:mod:`teatree.agents.runner`) drives an in-process agent
session behind a narrow protocol pair — :class:`Harness` opens a session for a
built set of options, :class:`HarnessSession` is the in-flight session surface the
driver talks to. :func:`resolve_harness` reads the DB-home ``agent_harness``
setting and returns the backend.

PR-1 (#2565, #2883) shipped :class:`ClaudeSdkHarness`, wrapping today's
``claude-agent-sdk`` ``ClaudeSDKClient`` — the default, so the transport is
byte-identical to before the seam existed. PR-2
([#2885](https://github.com/souliane/teatree/issues/2885)) ships the
provider-agnostic backend, :class:`PydanticAiHarness`: a Pydantic AI
:class:`~pydantic_ai.Agent` targeting the configured OpenAI-compatible,
metered endpoint. Both backends yield the SAME ``claude_agent_sdk`` message
vocabulary (``AssistantMessage`` / ``ResultMessage``) from :meth:`HarnessSession.receive_response`
so the driver (:func:`teatree.agents.runner._collect`) never special-cases the
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

import logging
from collections.abc import AsyncIterator
from contextlib import AbstractAsyncContextManager, AsyncExitStack, asynccontextmanager
from typing import TYPE_CHECKING, Protocol, cast

from claude_agent_sdk import ClaudeAgentOptions, ClaudeSDKClient
from pydantic_ai import Agent
from pydantic_ai.models import Model
from pydantic_ai.models.openai import OpenAIChatModel, ReasoningEffort

from teatree.agents.claude_cli_spawn import assert_spawnable, prepared_spawn, spawn_error
from teatree.agents.harness_options import HarnessOptions
from teatree.agents.harness_registry import (
    HarnessBuildContext,
    HarnessCapabilities,
    assert_provider_valid_for_harness,
    register_harness,
    resolve_harness_spec,
    valid_providers_for,
)
from teatree.agents.lane_b.config import LaneBToolConfig
from teatree.agents.lane_b.toolsets import build_lane_b_toolsets
from teatree.agents.model_tiering import HARNESS_EFFORT_SCALE, resolve_phase_harness, resolve_pydantic_ai_model
from teatree.agents.pydantic_ai_config import (
    PYDANTIC_AI_NATIVE_CAPABILITIES,
    PYDANTIC_AI_ROUTER_CAPABILITIES,
    OpenAICompatibleLaneConfig,
    PydanticAiBinding,
    PydanticAiModelConfig,
    build_model_settings,
    build_openai_compatible_provider,
    resolve_native_anthropic_model,
)
from teatree.agents.pydantic_ai_resume import persist_parked_thread, rehydrate_thread_for_resume
from teatree.agents.pydantic_ai_session import PydanticAiHarnessSession
from teatree.agents.regulated_path import RegulatedPathPolicy
from teatree.config import AgentHarness, AgentHarnessProvider, get_effective_settings
from teatree.llm.credentials import Credential

logger = logging.getLogger(__name__)

CLAUDE_SDK_CAPABILITIES = HarnessCapabilities(
    hooks=True,
    mcp=True,
    cache_control=False,
    server_resume=True,
    structured_output=False,
    spawns_cli_child=True,
    metered_lane=False,
)
if TYPE_CHECKING:
    from pydantic_ai.messages import ModelMessage

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
    """Opens a :class:`HarnessSession` for a built set of agent options.

    ``capabilities`` (#3157 E1) is the typed flag set the driver and doctors read instead of
    ``isinstance``-branching on the concrete backend class — including the dispatch-lane hints
    ``spawns_cli_child`` / ``metered_lane`` (#3157 AH-5), which the driver reads as typed
    fields through this attribute rather than by untyped ``getattr`` on the concrete class. So
    an overlay backend implements ``open`` + ``capabilities`` and the driver routes it purely
    off those flags. ``restore_unconsumed_resume_thread`` stays an OPTIONAL method hook (only a
    client-side-resumable backend implements it), read defensively by the driver.

    ``open`` deliberately takes the vendor ``claude_agent_sdk.ClaudeAgentOptions`` at the seam
    boundary (#3157 AH-2): the ``claude_sdk`` backend hands it straight to ``ClaudeSDKClient``,
    and re-homing the SDK-specific surface (``mcp_servers``, hooks, tool permissions) onto a
    fully-neutral ``open`` signature is the deferred strangler-fig migration — hence the boundary
    type is still the vendor one. A PROVIDER-AGNOSTIC backend must not thread that vendor type
    through its own logic: it adapts the vendor options into the neutral
    :class:`~teatree.agents.harness_options.HarnessOptions` ONCE at the top of ``open``
    (``HarnessOptions.from_sdk_options``, see :meth:`PydanticAiHarness.open`) and reads only
    neutral fields afterward, so ``ClaudeAgentOptions`` never leaks past the boundary.
    """

    @property
    def capabilities(self) -> HarnessCapabilities:
        """The typed flag set the driver routes on — READ-ONLY.

        Declared as a read-only property, not a mutable attribute: every consumer
        only reads it, and a bare attribute here silently excludes any backend that
        exposes it as a ``@property`` (``PydanticAiHarness`` does), because a
        read-only property cannot satisfy a settable protocol member. A plain class
        attribute — the ``claude_sdk`` backend's form — still satisfies this.
        """
        ...

    def open(self, options: ClaudeAgentOptions) -> AbstractAsyncContextManager[HarnessSession]: ...


class ClaudeSdkHarness:
    """The default backend — the ``claude-agent-sdk`` in-process transport.

    Declares its capabilities as a typed :class:`HarnessCapabilities` (#3157 E1/AH-5) so the
    driver reads them instead of ``isinstance``-branching: it spawns the bundled ``claude``
    CLI child (``spawns_cli_child`` → dispatch resolves the provider child env), authenticates
    on the subscription lane (``metered_lane`` is ``False`` — attribution comes from the
    explicit provider pin, see ``_resolve_dispatch_lane``), and resumes server-side via
    ``--resume``.
    """

    capabilities: HarnessCapabilities = CLAUDE_SDK_CAPABILITIES

    @staticmethod
    @asynccontextmanager
    async def open(options: ClaudeAgentOptions) -> AsyncIterator[HarnessSession]:
        """Spawn the CLI child with the system prompt on a FILE, and name an E2BIG death (#4301).

        Only the CONNECT is wrapped: an exception from the driver's own body must never be
        re-labelled "the agent could not start" when the agent plainly did.
        """
        with prepared_spawn(options) as spawn_options:
            payload = assert_spawnable(spawn_options)
            stack = AsyncExitStack()
            try:
                client = await stack.enter_async_context(ClaudeSDKClient(options=spawn_options))
            except Exception as exc:
                if (named := spawn_error(exc, payload)) is not None:
                    raise named from exc
                raise
            try:
                yield client
            finally:
                await stack.aclose()

    def restore_unconsumed_resume_thread(self) -> None:
        """No client-side resume thread to restore — server-side ``--resume`` owns it."""


def resolve_effort(options: HarnessOptions) -> ReasoningEffort | None:
    """Map the NEUTRAL ``options.effort`` onto pydantic_ai's ``ReasoningEffort`` vocabulary.

    Takes the neutral :class:`~teatree.agents.harness_options.HarnessOptions` (#3157 AH-2), not
    the vendor ``ClaudeAgentOptions`` — the effort axis is provider-agnostic, so the vendor type
    does not reach here. Public seam: the eval ``pydantic_ai`` runner
    (:mod:`teatree.eval.pydantic_ai_runner`) reuses this single effort-vocabulary guard so a
    agent dispatch and an eval run drop the same out-of-vocabulary rungs.

    ``options.effort`` is already scoped to the ACTIVE harness by
    :func:`teatree.agents.model_tiering.resolve_spawn_effort` (called while the SDK options were
    built), so this is normally a pass-through; the
    :data:`~teatree.agents.model_tiering.HARNESS_EFFORT_SCALE` re-check is a defence-in-depth
    guard for options built outside that resolver (e.g. a test), dropping an out-of-vocabulary
    value (``max``, the one rung ``claude_sdk`` has that ``pydantic_ai`` does not) rather than
    handing the model a reasoning-effort string it will reject.
    """
    effort = options.effort
    if effort is None or effort not in HARNESS_EFFORT_SCALE[AgentHarness.PYDANTIC_AI]:
        return None
    return cast("ReasoningEffort", effort)


class PydanticAiHarness:
    """The ``pydantic_ai`` backend — the generic OpenAI-compatible transport.

    ``open`` builds a fresh :class:`~pydantic_ai.Agent` from *options* (model,
    system prompt, and reasoning effort — MCP servers, hooks, and tool
    permissions are the ``claude-agent-sdk``-specific surface the strangler-fig
    migration re-homes in a later PR, per the redesign doc's port-surface table)
    targeting the configured OpenAI-compatible endpoint with the credential the
    ``openai_compatible_credential_entry`` setting names
    (:func:`~teatree.llm.openai_compatible.resolve_openai_compatible_backend`).

    *model* is INJECTABLE (default ``None`` triggers the real backend
    resolution lazily, INSIDE ``open`` — never at construction time, so building
    the harness never requires a live credential) so tests drive it with
    pydantic_ai's own :class:`~pydantic_ai.models.test.TestModel` /
    :class:`~pydantic_ai.models.function.FunctionModel` doubles, with no network
    and no :class:`~teatree.llm.credentials.CredentialError` risk. A resolved
    model name is checked against the regulated-path allowlist policy
    (:class:`~teatree.agents.regulated_path.RegulatedPathPolicy`, #2887)
    before it reaches the provider — a no-op unless the lane sets
    ``enforce_regulated_path``.

    *history* (#2886) is the rehydrated conversation of a RESUMED park, if
    any — passed straight through to the opened :class:`PydanticAiHarnessSession`
    so its first turn already carries the prior context. ``None``/absent (the
    default, and every non-resumed dispatch) opens a fresh empty conversation,
    byte-identical to before cached-resume existed.

    *resume_source* (souliane/teatree#2916) is the parked ``Task`` *history*
    was popped from, when this harness seeds a resume — ``None`` for a fresh
    dispatch. ``resolve_harness`` pops that thread the moment it BUILDS this
    harness, before ``open`` ever runs and resolves the backend credential
    — so a caller that refuses dispatch after construction but before a
    successful ``open`` (a budget breach, a credential failure) can restore
    the popped entry via :attr:`history` + *resume_source*.

    The dispatch-lane hints live on :attr:`capabilities` (#3157 AH-5): ``metered_lane`` is
    ``True`` (a ``pydantic_ai`` run always authenticates on the metered lane — the configured key
    or the native Anthropic key — the transport fixes it) and ``spawns_cli_child`` is ``False``
    (no bundled CLI child; the credential resolves in-process inside ``open``).
    """

    def __init__(
        self,
        *,
        model: Model | None = None,
        history: "list[ModelMessage] | None" = None,
        resume_source: "Task | None" = None,
        phase: str | None = None,
        config: PydanticAiModelConfig | None = None,
    ) -> None:
        self._model = model
        self._history = history
        self.resume_source = resume_source
        # *phase* opts the dispatch into the Lane-B tool layer (PR-03): a set
        # phase resolves the phase-scoped, gated toolsets (:mod:`teatree.agents.lane_b`).
        # ``None`` (the default) keeps a text-in/text-out Agent with no tools.
        self._phase = phase
        # The model-construction bundle (backend knobs + binding), resolved
        # SYNCHRONOUSLY by :func:`resolve_harness`. Absent → the defaults (router
        # binding, factory lane, uncapped, no pre-resolved regulated-path policy).
        cfg = config or PydanticAiModelConfig()
        self._backend = cfg.backend
        self._binding = cfg.binding
        self._max_tokens = cfg.max_tokens
        self._regulated_path = cfg.regulated_path
        self._anthropic_credential = cfg.anthropic_credential

    @property
    def history(self) -> "list[ModelMessage] | None":
        """The seed conversation this harness was constructed with, if any."""
        return self._history

    @property
    def binding(self) -> PydanticAiBinding:
        """Which model binding this harness constructs (router vs native Anthropic)."""
        return self._binding

    @property
    def capabilities(self) -> HarnessCapabilities:
        """This backend's capabilities — the native Anthropic binding adds ``cache_control``."""
        if self._binding is PydanticAiBinding.NATIVE_ANTHROPIC:
            return PYDANTIC_AI_NATIVE_CAPABILITIES
        return PYDANTIC_AI_ROUTER_CAPABILITIES

    def restore_unconsumed_resume_thread(self) -> None:
        """Re-persist a resume thread popped but never actually driven (#2916).

        ``resolve_harness`` pops a resumed task's parked thread as a side effect of BUILDING
        this harness — before ``open()`` (the only point the credential resolves) runs. When
        ``open()`` then fails, the popped thread would be silently lost even though the run
        never happened; this re-persists it. A no-op for a fresh (non-resumed) dispatch.
        """
        if self.resume_source is not None and self._history:
            persist_parked_thread(self.resume_source, self._history)

    def _resolve_model(self, options: HarnessOptions) -> Model:
        if self._model is not None:
            return self._model
        if self._binding is PydanticAiBinding.NATIVE_ANTHROPIC:
            return resolve_native_anthropic_model(options, self._regulated_path, self._anthropic_credential)
        # Normalise the resolved id to what the configured endpoint actually serves:
        # ``options.model`` is a teatree-abstract-tier default in Claude DASH-form
        # (the :data:`TIER_MODELS` form) an OpenAI-compatible provider does NOT carry, so it maps
        # to the configured ``openai_compatible_model``; an explicit provider-native pin
        # passes through.
        model_name = resolve_pydantic_ai_model(options.model, configured_model=self._backend.model)
        # Regulated-path allowlist gate on the ORIGINAL pin (before normalisation
        # laundered a bare ineligible id into the configured id) — a config-policy
        # refusal that must surface BEFORE the credential step, so it fires even when
        # the backend credential is absent. ``options.model`` catches both a bare
        # ineligible name and an explicit provider-prefixed pin (which passes through
        # normalisation unchanged); an absent pin falls back to the resolved id.
        RegulatedPathPolicy.resolve(self._regulated_path).assert_allowed(options.model or model_name)
        return OpenAIChatModel(model_name, provider=build_openai_compatible_provider(self._backend))

    @asynccontextmanager
    async def open(self, options: ClaudeAgentOptions) -> AsyncIterator[HarnessSession]:
        # AH-2: adapt the vendor options into the neutral HarnessOptions ONCE at the boundary,
        # then thread only the neutral type through the provider-agnostic build below — the
        # ``ClaudeAgentOptions`` type never reaches ``_resolve_model`` / ``resolve_effort`` /
        # the tool config, so the pydantic_ai (and future Vertex) path is vendor-type-free.
        harness_options = HarnessOptions.from_sdk_options(options)
        model = self._resolve_model(harness_options)
        # The effort key is BINDING-specific (``openai_reasoning_effort`` vs
        # ``anthropic_effort``) and a foreign key is dropped silently, so the settings
        # are built per binding — see :func:`build_model_settings`.
        model_settings = build_model_settings(
            model, resolve_effort(harness_options), binding=self._binding, max_tokens=self._max_tokens
        )
        # PR-03: a phased dispatch wires the phase-scoped, gated tool/MCP layer
        # onto the Agent (``toolsets=`` + ``tool_timeout=``); an un-phased one
        # keeps a bare text Agent (byte-identical to before the tool port). The
        # worktree jail root is ``options.cwd`` (the resolved task cwd).
        config = LaneBToolConfig.from_options(harness_options, phase=self._phase or "")
        toolsets = build_lane_b_toolsets(config).toolsets if self._phase else []
        agent: Agent[None, str] = Agent(
            model,
            system_prompt=harness_options.system_prompt,
            model_settings=model_settings,
            toolsets=toolsets,
            tool_timeout=config.shell_timeout_seconds if self._phase else None,
        )
        # ``async with agent:`` enters the model so the provider's HTTP client
        # (the OpenAI-compatible connection pool) closes cleanly on
        # exit — a bare ``Agent(...)`` never closes it, leaking a client per
        # dispatch until GC.
        # A positive caller ``max_turns`` (an OneShotSpec cap, an eval override) wins over the
        # lane's own ``request_limit``; ``0`` (a agent dispatch, an SDK-``None`` coercion)
        # keeps ``request_limit`` — so every uncapped dispatch stays byte-identical.
        request_limit = harness_options.max_turns if harness_options.max_turns > 0 else self._backend.request_limit
        async with agent:
            yield PydanticAiHarnessSession(
                agent,
                model_name=model.model_name,
                history=self._history,
                phase=self._phase,
                request_limit=request_limit,
            )


def _build_claude_sdk_harness(context: HarnessBuildContext) -> Harness:  # noqa: ARG001 — factory signature
    """The built-in ``claude_sdk`` factory — a stateless :class:`ClaudeSdkHarness`."""
    return ClaudeSdkHarness()


def _routed_anthropic_credential(binding: PydanticAiBinding, task: "Task | None") -> Credential | None:
    """The per-account metered credential the NATIVE Anthropic binding authenticates with.

    ``None`` for the ROUTER binding, which authenticates through the OpenAI-compatible
    credential entry instead and must never pay for an Anthropic account probe. For the
    native binding this is the SAME ``anthropic_api_key_pass_paths`` selector the
    ``claude_sdk`` lane routes through (:func:`~teatree.credential_config.resolve_api_key_credential`),
    at the task's overlay scope — one routing seam for both transports, never a second,
    weaker lookup. Returns a credential carrying only the selected store PATH.
    """
    if binding is not PydanticAiBinding.NATIVE_ANTHROPIC:
        return None
    from teatree.credential_config import resolve_api_key_credential  # noqa: PLC0415 — deferred: ORM-backed selector

    return resolve_api_key_credential(scope=_task_overlay(task) or "")


def _build_pydantic_ai_harness(context: HarnessBuildContext) -> Harness:
    """The built-in ``pydantic_ai`` factory ([#2885](https://github.com/souliane/teatree/issues/2885)).

    Resolves the OpenAI-compatible backend knobs SYNCHRONOUSLY (the ``x-lane`` value, the
    endpoint, the model id, the per-run step cap, the credential-store entry) rather than
    inside the async ``open`` where a DB read fails safe to defaults, rehydrates any
    resumable ancestor's parked thread, and selects the model binding from
    ``agent_harness_provider``: ``anthropic_api`` → the native Anthropic Messages-API binding
    (#3157 E1b, real ``cache_control``), else the generic OpenAI-compatible binding.

    The NATIVE binding's metered credential is routed here for that same reason, and it is
    the ONLY reason it cannot be left to ``open``: the per-account selector reads
    ``ConfigSetting`` / ``AnthropicTokenUsage`` rows, and a Django ORM read inside
    ``asyncio.run`` raises ``SynchronousOnlyOperation``. It yields a credential carrying the
    selected ``pass`` PATH — no secret and no network read here; the key itself is resolved
    lazily by :func:`~teatree.agents.pydantic_ai_config.resolve_native_anthropic_model`
    inside ``open``, where the existing ``CredentialError`` seam records the failure. Routing
    is resolved at the TASK's OVERLAY scope, the same scope the settings above come from, so
    the transport and its credential can never be read from two different scopes.

    The rehydration POPS the ancestor's entry (single-use), so the built harness's
    ``resume_source`` records which ancestor it came from — a caller that refuses the
    dispatch before ``open`` genuinely runs restores it via
    :meth:`PydanticAiHarness.restore_unconsumed_resume_thread` (souliane/teatree#2916).
    """
    settings = context.settings if context.settings is not None else get_effective_settings()
    resumed = rehydrate_thread_for_resume(context.task) if context.task is not None else None
    binding = (
        PydanticAiBinding.NATIVE_ANTHROPIC
        if settings.agent_harness_provider is AgentHarnessProvider.ANTHROPIC_API
        else PydanticAiBinding.ROUTER
    )
    return PydanticAiHarness(
        history=resumed.history if resumed else None,
        resume_source=resumed.ancestor if resumed else None,
        phase=context.phase,
        config=PydanticAiModelConfig(
            binding=binding,
            max_tokens=settings.pydantic_ai_max_tokens,
            regulated_path=RegulatedPathPolicy.from_settings(settings),
            anthropic_credential=_routed_anthropic_credential(binding, context.task),
            backend=OpenAICompatibleLaneConfig(
                lane=settings.openai_compatible_lane,
                request_limit=settings.pydantic_ai_request_limit,
                base_url=settings.openai_compatible_base_url,
                credential_entry=settings.openai_compatible_credential_entry or None,
                model=settings.openai_compatible_model or None,
            ),
        ),
    )


register_harness(
    AgentHarness.CLAUDE_SDK.value,
    _build_claude_sdk_harness,
    capabilities=CLAUDE_SDK_CAPABILITIES,
    valid_providers=frozenset({AgentHarnessProvider.SUBSCRIPTION_OAUTH.value, AgentHarnessProvider.API_KEY.value}),
)
register_harness(
    AgentHarness.PYDANTIC_AI.value,
    _build_pydantic_ai_harness,
    capabilities=PYDANTIC_AI_ROUTER_CAPABILITIES,
    valid_providers=frozenset({AgentHarnessProvider.OPENAI_COMPATIBLE.value, AgentHarnessProvider.ANTHROPIC_API.value}),
)


def _task_overlay(task: "Task | None") -> str | None:
    """The overlay name a dispatch's config resolves under — the task's ticket overlay.

    ``None`` (no task, or a task whose ticket carries no overlay) keeps the
    active-overlay resolution so the interactive/default path is unchanged.
    """
    if task is None:
        return None
    return task.ticket.overlay or None


def resolve_harness(task: "Task | None" = None, *, phase: str | None = None) -> Harness:
    """Return the headless transport backend selected by the OPEN ``agent_harness`` setting.

    Looks the resolved harness NAME up in the registry (#3157 E1) and builds it through the
    registered factory — the backend set is no longer a closed enum, so an overlay-registered
    third transport dispatches with ZERO core edits. Defaults to ``claude_sdk``
    (byte-identical to today). An unregistered name raises
    :class:`~teatree.agents.harness_registry.UnknownHarnessError` (caught and recorded as a
    dispatch failure by ``_resolve_backend_or_failure``).

    *task* / *phase* are threaded into the :class:`HarnessBuildContext` the factory reads:
    the ``pydantic_ai`` factory rehydrates *task*'s resumable ancestor thread and opts *phase*
    into the Lane-B tool layer; the ``claude_sdk`` factory ignores both.

    The configured ``agent_harness`` is first run through
    :func:`~teatree.agents.model_tiering.resolve_phase_harness`, which PINS a verification
    *phase* to ``claude_sdk`` regardless of the setting (the metered-lane guardrail
    #2) — so when a MAKER phase rides a cheap model on ``pydantic_ai``, the checker stays on
    the trusted Claude lane. A verification phase therefore never rehydrates a pydantic_ai
    resume thread (its factory is the claude_sdk one).

    Settings are resolved at the TASK's OVERLAY scope (``task.ticket.overlay``), not
    global/active-only: whether an overlay runs Lane B (``agent_harness=pydantic_ai``)
    and its endpoint / credential / request cap are all per-overlay overridable, and a
    agent dispatch runs per-task, so a per-overlay override for a NON-active overlay
    must apply. A task-less ``resolve_harness()`` (the interactive/default path) keeps
    the active-overlay resolution (env layer included).

    Before building, the CONFIGURED ``(agent_harness, agent_harness_provider)`` pair is
    validated against the resolved backend's registry-declared ``valid_providers`` (#3157
    AH-6) — a live consumer that also enforces an overlay-registered backend's own provider
    constraint, which the closed-enum ``AgentHarnessProvider.valid_for`` cannot. It validates
    the CONFIG harness (never the phase-pinned one), so a verification-phase pin never turns a
    provider valid for the configured harness into a spurious failure; an unpinned provider
    always passes. The dispatch's Layer-2 credential must therefore come from
    :func:`resolve_dispatch_provider`, which applies the SAME pin to the provider — reading
    ``settings.agent_harness_provider`` directly re-opens exactly the failure this validation
    deliberately declines to raise.
    """
    settings = get_effective_settings(_task_overlay(task))
    provider = settings.agent_harness_provider
    assert_provider_valid_for_harness(settings.agent_harness, provider.value if provider is not None else None)
    harness_name = resolve_phase_harness(settings.agent_harness, phase)
    spec = resolve_harness_spec(harness_name)
    return spec.factory(HarnessBuildContext(task=task, phase=phase, settings=settings))


def resolve_dispatch_provider(task: "Task | None" = None, *, phase: str | None = None) -> AgentHarnessProvider | None:
    """The Layer-2 provider that APPLIES to the harness :func:`resolve_harness` just resolved.

    ``agent_harness_provider`` is Layer 2 and is CONSTRAINED BY Layer 1: the operator pins
    it for the ``agent_harness`` they configured. When
    :func:`~teatree.agents.model_tiering.resolve_phase_harness` PINS a verification *phase*
    onto a different transport, that Layer-1 flip does not carry the Layer-2 pin with it —
    the pin was never made for the pinned harness. Reading the configured provider straight
    off the settings would hand the dispatch a credential selector invalid under the harness
    it is actually running, which the claude_sdk child-env resolver
    (:func:`~teatree.agents._runner_env._provider_child_env`) then refuses, failing every
    verification dispatch of an otherwise-VALID deployment.

    So a pin the phase flip invalidated is DROPPED (to the ambient-credential default,
    ``None``) with a WARNING — never silently, and never by inventing a substitute
    credential the operator did not choose. This mirrors
    :func:`~teatree.agents._runner_env.system_child_env`, which already warns-and-falls-back
    for the same shape.

    Nothing else is weakened. A pair no phase pin explains is untouched here and still fails
    loud: the ``ConfigSetting`` cross-key gate
    (:func:`~teatree.config.cross_key_consistency.validate_cross_key_write`) refuses to store
    it, and :func:`resolve_harness` above raises
    :class:`~teatree.agents.harness_registry.InvalidHarnessProviderError` on it at dispatch.

    Resolved at the same TASK-OVERLAY settings scope as :func:`resolve_harness`, so the
    transport and the credential can never be read from two different scopes.
    """
    settings = get_effective_settings(_task_overlay(task))
    provider = settings.agent_harness_provider
    if provider is None:
        return None
    harness_name = resolve_phase_harness(settings.agent_harness, phase)
    if harness_name == settings.agent_harness:
        return provider
    valid = valid_providers_for(harness_name)
    if valid and provider.value not in valid:
        logger.warning(
            "phase=%s pins agent_harness=%s, under which the configured "
            "agent_harness_provider=%s is not valid; this dispatch drops the Layer-2 pin and "
            "uses the ambient credential (the configured agent_harness=%s is unaffected)",
            phase,
            harness_name,
            provider.value,
            settings.agent_harness,
        )
        return None
    return provider


def pydantic_ai_thread(session: HarnessSession) -> "list[ModelMessage] | None":
    """The session's conversation when *session* is pydantic_ai-backed, else ``None`` (#2886)."""
    return session.history if isinstance(session, PydanticAiHarnessSession) else None
