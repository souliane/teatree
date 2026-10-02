"""Non-Claude eval execution over the provider-agnostic ``pydantic_ai`` harness seam.

The third :class:`~teatree.eval.backends.EvalRunner`, and the model-evolution
unblock. Where the ``api`` backend runs the Claude CLI via ``claude-agent-sdk`` and
``transcript`` replays recorded Claude Code JSONL, this backend drives a
``pydantic_ai`` :class:`~pydantic_ai.Agent` (OpenAI-compatible) so
the behavioral eval lane can grade a **non-Claude** model — a GPT/open-source swap
becomes a config change (``agent_harness`` + a tier-model/router row), not a code
change, and a swapped model is no longer unverifiable.

The grader path stays runtime-neutral because it is the SAME seam the other two
backends use: :class:`~teatree.agents.harness.PydanticAiHarnessSession` already
adapts pydantic_ai's streamed output into the ``claude_agent_sdk`` message
vocabulary every backend yields, and :func:`~teatree.eval.message_mapping.eval_run_from_messages`
folds those typed messages into an :class:`~teatree.eval.models.EvalRun` unchanged.
The matchers and judge never see the transport.

The scenario's declared tools advertise the production parameter schema. Most are
inert stubs because the eval grades the tool CALL. A declared fixture makes ``Bash``
and ``Edit`` real within that throwaway tree, so an edit-and-verify scenario can
observe its own change.

The exception is a scenario that DECLARED a sandbox (``fixture`` / ``cli_stubs``), where
``Bash`` runs for real inside it (:mod:`teatree.eval.eval_sandbox`) and a declared
fixture also permits ``Edit``. These declarations describe the world the prompt
presupposes; inert fixture tools make the agent spend its turns discovering it is absent.

When a scenario declares ``production_hooks``, the toolset is additionally wrapped by
the shipped PreToolUse manifest adapter.  Its command hooks see the live model response
and can deny a call before either an inert stub or a sandbox tool executes.
"""

import asyncio
from collections.abc import Callable
from contextlib import ExitStack, nullcontext
from dataclasses import dataclass
from typing import Literal, cast, get_args

from claude_agent_sdk import Message
from claude_agent_sdk.types import EffortLevel
from pydantic_ai import Agent
from pydantic_ai.models import Model
from pydantic_ai.models.anthropic import AnthropicEffort, AnthropicModelSettings
from pydantic_ai.models.openai import OpenAIChatModel, OpenAIChatModelSettings, ReasoningEffort
from pydantic_ai.settings import ModelSettings
from pydantic_ai.tools import Tool
from pydantic_ai.toolsets import FunctionToolset

from teatree.agents.harness import resolve_effort
from teatree.agents.harness_options import HarnessOptions
from teatree.agents.model_tiering import model_supports_thinking, resolve_pydantic_ai_model
from teatree.agents.pydantic_ai_config import LANE_EVAL, OpenAICompatibleLaneConfig, build_openai_compatible_provider
from teatree.agents.pydantic_ai_session import PydanticAiHarnessSession
from teatree.agents.pydantic_ai_turn import SessionRun
from teatree.agents.regulated_path import assert_model_allowed_on_regulated_path
from teatree.agents.runner_failure_taxonomy import TURN_CEILING_SUBTYPE
from teatree.config import get_effective_settings
from teatree.config.settings import PYDANTIC_AI_MAX_TOKENS_DEFAULT
from teatree.eval.api_errors import classify_transient_throttle
from teatree.eval.api_runner import load_agent_definition
from teatree.eval.eval_sandbox import (
    BASH_TOOL,
    EDIT_TOOL,
    EvalSandbox,
    provision_eval_sandbox,
    sandbox_bash_tool,
    sandbox_edit_tool,
)
from teatree.eval.harness_failure import HOOKS_NOT_REGISTERED_REASON
from teatree.eval.message_mapping import eval_run_from_messages
from teatree.eval.model_resolution import resolve_spec_model
from teatree.eval.model_variant import parse_model_variant
from teatree.eval.models import CLEAN_ROOM_LANE, CLEAN_ROOM_MIN_TURNS, EvalRun, EvalSpec, canonicalize_tool
from teatree.eval.production_hook_bridge import ProductionHookBridge, ProductionHookToolset
from teatree.eval.production_hooks import has_hook_events
from teatree.eval.prompt_framing import LIVE_ENV_FRAMING
from teatree.eval.resource_caps import resolve_watchdog_seconds
from teatree.eval.throttle_retry import ThrottleRetryDriver, ThrottleRetryHandlers, resolve_throttle_retries
from teatree.eval.under_load import build_system_prompt, build_user_prompt
from teatree.llm.builtin_tool_schemas import BUILTIN_TOOL_PARAMETERS

#: ``pydantic_ai``'s own provider discriminator for the Anthropic transport
#: (``AnthropicModel.system``) — the branch key for which settings class a model reads.
_ANTHROPIC_SYSTEM = "anthropic"

#: Prompt-cache TTL for the eval lane's system instructions. Every scenario sharing an
#: ``agent_path`` sends a byte-identical system prompt, and the shortest TTL refreshes on
#: each read, so a suite run keeps the entry warm at the cheaper 5-minute write rate.
EVAL_CACHE_TTL: Literal["5m"] = "5m"


def _inert_tool(**_kwargs: object) -> str:
    """A stub tool body: accept any arguments the model passes, return nothing.

    The eval measures the CALL, not the effect — the harness session captures the
    model's ``ToolCallPart`` in the ``ToolUseBlock`` vocabulary the grader reads, so
    a benign empty return keeps the conversation flowing with no side effect.
    """
    return ""


def build_eval_toolset(tool_names: tuple[str, ...], sandbox: EvalSandbox | None = None) -> FunctionToolset[None]:
    """A ``pydantic_ai`` toolset of declared tools, usually inert stubs.

    Each of *tool_names* (``EvalSpec.tools``) becomes a stub that advertises the
    PRODUCTION tool's parameter schema (:data:`~teatree.llm.builtin_tool_schemas.BUILTIN_TOOL_PARAMETERS`)
    while usually executing nothing, so the model can issue the call the matchers grade.

    A declared sandbox runs ``Bash`` inside its provisioned directory. ``Edit`` also
    executes when that sandbox has a fixture, so edits are visible to later shell
    checks. A stub-only sandbox and a scenario with no sandbox keep ``Edit`` inert.

    Registered from the schema rather than from ``_inert_tool``'s own signature: a
    ``**kwargs`` body infers ZERO properties, which tells the model nothing about
    what any tool takes. It still emits ``Bash({"command": …})`` from training
    priors, but it cannot invent a STRUCTURED shape it was never shown, so it
    calls ``AskUserQuestion({})`` and every ``args.questions`` matcher reds a
    correctly-behaving agent.

    ``Tool.from_schema`` validates arguments with an ``any`` schema, so the
    advertised shape is a hint to the model and never a filter on the captured
    call — an unmodelled tool keeps the fully permissive stub, and an argument
    outside a curated shape still reaches the grader.
    """
    toolset: FunctionToolset[None] = FunctionToolset()
    for name in tool_names:
        body = _tool_body(name, sandbox)
        parameters = BUILTIN_TOOL_PARAMETERS.get(name)
        if parameters is None:
            toolset.add_function(body, name=name)
        else:
            toolset.add_tool(Tool.from_schema(body, name=name, description=None, json_schema=parameters.json_schema()))
    return toolset


def _tool_body(name: str, sandbox: EvalSandbox | None) -> Callable[..., str]:
    """Use real fixture tools where declared; otherwise keep the call inert."""
    if sandbox is not None and canonicalize_tool(name) == BASH_TOOL:
        return sandbox_bash_tool(sandbox)
    if sandbox is not None and sandbox.can_edit and canonicalize_tool(name) == EDIT_TOOL:
        return sandbox_edit_tool(sandbox)
    return _inert_tool


def _system_prompt(spec: EvalSpec) -> str:
    """The clean-room system prompt: the agent definition + the live-env framing.

    Identical construction to the ``api`` lane (:mod:`teatree.eval.api_runner`) so a
    scenario grades the SAME agent definition regardless of the backend.
    """
    clean_room_prompt = (
        load_agent_definition(spec.agent_path, spec.agent_sections, spec.source_path.parent) + LIVE_ENV_FRAMING
    )
    return build_system_prompt(spec, clean_room_prompt=clean_room_prompt)


def _anthropic_settings(resolved: ReasoningEffort | None, max_tokens: int) -> AnthropicModelSettings:
    """Anthropic-keyed settings: the output ceiling, the instruction cache, the effort.

    ``AnthropicEffort`` carries no ``minimal`` rung, so the vocabulary is re-checked
    against the provider's own scale here — the harness guard only narrows to what
    ``pydantic_ai`` accepts, which is the wider set. A rung Anthropic has no name for
    is dropped; the ceiling and the cache key still ride.
    """
    settings = AnthropicModelSettings(anthropic_cache_instructions=EVAL_CACHE_TTL)
    if max_tokens > 0:
        settings["max_tokens"] = max_tokens
    if resolved in get_args(AnthropicEffort):
        settings["anthropic_effort"] = cast("AnthropicEffort", resolved)
    return settings


def _openai_settings(resolved: ReasoningEffort | None, max_tokens: int) -> OpenAIChatModelSettings | None:
    """OpenAI-keyed settings: the output ceiling plus the reasoning effort."""
    settings = OpenAIChatModelSettings()
    if max_tokens > 0:
        settings["max_tokens"] = max_tokens
    if resolved is not None:
        settings["openai_reasoning_effort"] = resolved
    return settings or None


def _model_settings(model: Model, effort: EffortLevel | None, max_tokens: int) -> ModelSettings | None:
    """The settings *model*'s provider actually reads: output ceiling, cache, effort.

    ``max_tokens`` is a base :class:`~pydantic_ai.settings.ModelSettings` key both
    bindings honour on the wire. Left unset, the Anthropic binding falls back to 4096
    and truncates a long graded result envelope mid-JSON — on the lane whose whole job
    is grading those envelopes. ``0`` is the documented escape hatch on
    ``pydantic_ai_max_tokens`` and leaves the binding's own default.

    ``AnthropicModel`` reads ``anthropic_effort`` and has no ``openai_reasoning_effort``
    in its vocabulary at all — an OpenAI-keyed effort handed to it is accepted and
    discarded, so the run silently drops to the provider default while the report still
    names the pinned rung. The branch key is ``pydantic_ai``'s own provider
    discriminator (:data:`_ANTHROPIC_SYSTEM`), never the model id. The instruction-cache
    key is Anthropic-namespaced and rides only that branch.

    Reuses the harness's effort-vocabulary guard (:func:`~teatree.agents.harness.resolve_effort`)
    so the ``pydantic_ai`` lane drops an out-of-vocabulary rung (``max``) exactly as
    a agent dispatch does, rather than handing the provider a level it rejects.

    That vocabulary guard narrows the LEVEL; it says nothing about whether the resolved
    MODEL carries the lever at all, and the two failures look nothing alike. Haiku
    answers a request carrying any effort with ``400 invalid_request_error: This model
    does not support the effort parameter`` -- a whole-request rejection, so the run
    captures an EMPTY trajectory and the scenario reds as ``error_during_execution``
    having graded nothing. That is what the ``--preset baseline`` lane hits: the preset
    pins its cheapest-passing scenarios to the ``cheap``/Haiku tier while the lane-level
    :data:`~teatree.eval.resource_caps.METERED_DEFAULT_EFFORT` rides on every scenario,
    so 22 of 266 baseline scenarios could never execute. The capability guard is
    :func:`~teatree.agents.model_tiering.model_supports_thinking`, whose contract covers
    BOTH levers ("the cheap/Haiku tier rejects the ``thinking`` / ``effort`` levers") and
    which an agent dispatch already consults -- matched on the tier, so a future dated
    Haiku id is covered and a non-Claude id keeps its effort.

    Deliberately NOT routed through the harness lane's
    :func:`~teatree.agents.pydantic_ai_config.build_model_settings`: that builder branches
    on the harness's ``PydanticAiBinding`` enum rather than on a ``Model`` (the eval lane
    is handed injectable model doubles), maps efforts through
    ``ANTHROPIC_THINKING_EFFORT_MAP`` where this lane drops an out-of-vocabulary rung, and
    carries no cache key. Unifying them means changing the effort actually sent.
    """
    resolved = resolve_effort(HarnessOptions(effort=effort)) if model_supports_thinking(model.model_name) else None
    if model.system == _ANTHROPIC_SYSTEM:
        return _anthropic_settings(resolved, max_tokens)
    return _openai_settings(resolved, max_tokens)


@dataclass(frozen=True, slots=True)
class EvalDriveCaps:
    """What bounds ONE eval drive, shared by both fresh-run lanes (composition).

    *   ``turn_cap`` — an explicit ``--max-turns``; ``None`` defers to the scenario's own
        ``max_turns`` budget, tightened by the backend's per-run request-loop guardrail.
    *   ``effort`` — the lane-level representative reasoning effort, applied when a
        scenario declares no ``model@effort`` of its own (a declared effort wins).
    *   ``max_tokens`` — the per-request output-token ceiling. Defaulted rather than
        ``None`` so no construction path can silently fall back to the Anthropic
        binding's 4096, which truncates a long graded result envelope mid-JSON. ``0``
        is the documented escape hatch and leaves the binding's own default.
    """

    turn_cap: int | None = None
    effort: EffortLevel | None = None
    max_tokens: int = PYDANTIC_AI_MAX_TOKENS_DEFAULT


class TransientThrottleTerminusError(Exception):
    """A drive whose TERMINUS is a provider throttle, re-raised so the retry envelope sees it.

    ``PydanticAiHarnessSession`` maps a provider failure to a terminal ``is_error``
    message rather than raising — the agent harness needs that, because a dispatched
    agent must record what it did before the provider refused. For an EVAL that
    mapping is wrong on its own: a 529 ``overloaded_error`` is capacity, not
    behaviour, and grading it as ``error_during_execution`` reports a red the agent
    never earned. Converting only a THROTTLE terminus back into an exception hands it
    to the same bounded envelope the SDK lane already rides out, and leaves every
    other terminus (a cap, a refusal, a real behavioural fail) graded exactly as before.
    """


def _grade(spec: EvalSpec, messages: list[Message], bridge: ProductionHookBridge | None) -> EvalRun:
    # Gated calls that produced no hook event mean the shipped plugin never
    # registered: grading would score the raw model as the system under test.
    if bridge is not None and bridge.saw_gated_calls and not has_hook_events(messages):
        return EvalRun.terminal(spec.name, terminal_reason=HOOKS_NOT_REGISTERED_REASON)
    return eval_run_from_messages(spec, messages)


def _throttle_terminus(messages: list[Message]) -> str | None:
    """The error text of *messages*' terminus when it is a retriable throttle, else ``None``."""
    for message in reversed(messages):
        if getattr(message, "is_error", False):
            if getattr(message, "subtype", "") == TURN_CEILING_SUBTYPE:
                return None
            text = str(getattr(message, "result", "") or "")
            return text if classify_transient_throttle(text) is not None else None
    return None


class PydanticAiRunner:
    """Run an :class:`EvalSpec` through the ``pydantic_ai`` harness — the non-Claude lane.

    *model* is INJECTABLE (default ``None`` resolves the real backend model lazily
    inside :meth:`run`, so building the runner never needs a live credential): a test
    drives it with pydantic_ai's own :class:`~pydantic_ai.models.test.TestModel` /
    :class:`~pydantic_ai.models.function.FunctionModel` doubles, no network, no token.
    """

    def __init__(
        self,
        *,
        model: Model | None = None,
        caps: EvalDriveCaps | None = None,
        backend: OpenAICompatibleLaneConfig | None = None,
        retry: ThrottleRetryDriver | None = None,
    ) -> None:
        self._model = model
        self._caps = caps or EvalDriveCaps()
        self._backend = backend or OpenAICompatibleLaneConfig(lane=LANE_EVAL)
        # ``timeout_max_attempts=0`` keeps a watchdog hang surfacing on the first
        # timeout, exactly as this lane always has — the envelope is added for
        # provider throttles, and re-driving a hang would multiply the watchdog.
        self._retry = retry or ThrottleRetryDriver(max_attempts=resolve_throttle_retries(), timeout_max_attempts=0)

    def run(self, spec: EvalSpec) -> EvalRun:
        # Resolve the abstract tier/phase to a concrete model id (a no-op when the
        # spec already carries a concrete ``model``); the resolved id flows into the
        # variant parse, the model-presence check, the ledger label, and the report.
        spec = resolve_spec_model(spec)
        session_run = SessionRun.start(request_limit=self._resolve_request_limit(spec))
        model = self._resolve_model(spec, session_run)
        # Provision synchronously for EACH attempt. An Edit or Bash call may change
        # the fixture before a provider throttle, and the retried conversation must
        # start from the same initial files rather than the abandoned attempt's edits.
        with ExitStack() as attempt_scope:
            bridge: ProductionHookBridge | None = None

            def drive_attempt() -> list[Message]:
                nonlocal bridge
                attempt_scope.close()
                sandbox = attempt_scope.enter_context(provision_eval_sandbox(spec))
                bridge_context = (
                    ProductionHookBridge.for_spec(spec, run=session_run, cwd=sandbox.cwd if sandbox else None)
                    if spec.production_hooks
                    else nullcontext(None)
                )
                bridge = attempt_scope.enter_context(bridge_context)
                return self._drive_once(spec, model, sandbox, session_run, bridge)

            return self._retry.run(
                drive_attempt,
                ThrottleRetryHandlers(
                    grade_success=lambda messages, _retries: _grade(spec, messages, bridge),
                    grade_cap=lambda cap: EvalRun.terminal(spec.name, terminal_reason=cap.terminal_reason),
                    grade_mislabel=lambda mislabel: _grade(spec, mislabel.messages, bridge),
                    surface_throttled=lambda reason, _attempts: EvalRun.terminal(spec.name, terminal_reason=reason),
                ),
            )

    def _drive_once(
        self,
        spec: EvalSpec,
        model: Model,
        sandbox: EvalSandbox | None,
        session_run: SessionRun,
        bridge: ProductionHookBridge | None,
    ) -> list[Message]:
        """One attempt; a throttle TERMINUS is raised so the envelope can ride it out."""
        messages = asyncio.run(self._drive_with_watchdog(spec, model, sandbox, session_run, bridge))
        throttle = _throttle_terminus(messages)
        if throttle is not None:
            raise TransientThrottleTerminusError(throttle)
        return messages

    def _resolve_model(self, spec: EvalSpec, session_run: SessionRun) -> Model:
        if self._model is not None:
            return self._model
        pinned = parse_model_variant(spec.model).model
        resolved = resolve_pydantic_ai_model(pinned, configured_model=self._backend.model)
        assert_model_allowed_on_regulated_path(pinned or resolved)
        return OpenAIChatModel(resolved, provider=build_openai_compatible_provider(self._backend, session_run))

    def _resolve_request_limit(self, spec: EvalSpec) -> int | None:
        """An explicit ``--max-turns`` wins; else the scenario budget, tightened by the lane guardrail.

        The clean-room floor mirrors the SDK lane
        (:meth:`teatree.eval.api_runner.ApiInProcessRunner._resolve_max_turns`): honouring the raw
        declaration would red every catalog scenario that declares ``max_turns: 3``.
        """
        if self._caps.turn_cap is not None:
            return self._caps.turn_cap
        scenario_cap = max(spec.max_turns, CLEAN_ROOM_MIN_TURNS) if spec.lane == CLEAN_ROOM_LANE else spec.max_turns
        lane_cap = self._backend.request_limit
        if lane_cap is None or lane_cap <= 0:
            return scenario_cap
        return min(scenario_cap, lane_cap)

    async def _drive_with_watchdog(
        self,
        spec: EvalSpec,
        model: Model,
        sandbox: EvalSandbox | None,
        session_run: SessionRun,
        bridge: ProductionHookBridge | None,
    ) -> list[Message]:
        watchdog = spec.watchdog_seconds if spec.watchdog_seconds is not None else resolve_watchdog_seconds()
        return await asyncio.wait_for(self._drive(spec, model, sandbox, session_run, bridge), timeout=watchdog)

    async def _drive(
        self,
        spec: EvalSpec,
        model: Model,
        sandbox: EvalSandbox | None,
        session_run: SessionRun,
        bridge: ProductionHookBridge | None,
    ) -> list[Message]:
        variant = parse_model_variant(spec.model)
        effort = variant.effort if variant.effort is not None else self._caps.effort
        toolset = build_eval_toolset(spec.tools, sandbox)
        if bridge is not None:
            toolset = ProductionHookToolset(toolset, bridge=bridge)
        agent: Agent[None, str] = Agent(
            model,
            system_prompt=_system_prompt(spec),
            model_settings=_model_settings(model, effort, self._caps.max_tokens),
            toolsets=[toolset],
        )
        # ``async with agent`` enters the model so the provider's HTTP client closes
        # cleanly on exit rather than leaking one per run.
        async with agent:
            session = PydanticAiHarnessSession(
                agent,
                model_name=model.model_name,
                run=session_run,
            )
            if bridge is not None:
                session.observe_transport_hooks(bridge.observe, bridge.events)
            await session.query(build_user_prompt(spec))
            messages = [cast("Message", message) async for message in session.receive_response()]
            if bridge is not None:
                bridge.flush_transcript()
            return messages


def build_pydantic_ai_eval_runner(
    *,
    max_turns_override: int | None = None,
    effort: EffortLevel | None = None,
) -> PydanticAiRunner:
    """Build the ``pydantic_ai`` eval runner with the eval-lane backend knobs.

    The DB-home backend settings (the per-run step cap, the output-token ceiling, the
    endpoint, the model id, the credential-store entry, the extra headers) are resolved SYNCHRONOUSLY here
    — never inside the async ``run``, where a ``get_effective_settings`` read fails safe
    to defaults under Django's async guard — and pinned to the ``eval`` dispatch lane
    (``x-lane: eval``). This mirrors :func:`teatree.agents.harness.resolve_harness`.
    """
    settings = get_effective_settings()
    return PydanticAiRunner(
        caps=EvalDriveCaps(turn_cap=max_turns_override, effort=effort, max_tokens=settings.pydantic_ai_max_tokens),
        backend=OpenAICompatibleLaneConfig(
            lane=LANE_EVAL,
            request_limit=settings.pydantic_ai_request_limit,
            base_url=settings.openai_compatible_base_url,
            credential_entry=settings.openai_compatible_credential_entry or None,
            model=settings.openai_compatible_model or None,
            extra_headers=dict(settings.openai_compatible_extra_headers),
        ),
    )


__all__ = ["EvalDriveCaps", "PydanticAiRunner", "build_eval_toolset", "build_pydantic_ai_eval_runner"]
