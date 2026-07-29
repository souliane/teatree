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

The scenario's declared tools are registered as INERT stubs (:func:`build_eval_toolset`):
the eval grades the tool CALL the model issues, never its execution — exactly like
the clean-room ``api`` lane runs in an isolated sandbox — so a tool-call scenario is
captured in the same ``ToolUseBlock`` vocabulary the SDK lane produces, with no real
side effect.
"""

import asyncio
from dataclasses import dataclass
from typing import Literal, cast, get_args

from claude_agent_sdk import Message
from claude_agent_sdk.types import EffortLevel
from openai import AsyncOpenAI
from pydantic_ai import Agent
from pydantic_ai.models import Model
from pydantic_ai.models.anthropic import AnthropicEffort, AnthropicModelSettings
from pydantic_ai.models.openai import OpenAIChatModel, OpenAIChatModelSettings, ReasoningEffort
from pydantic_ai.providers.openai import OpenAIProvider
from pydantic_ai.settings import ModelSettings
from pydantic_ai.toolsets import FunctionToolset

from teatree.agents.harness import resolve_effort
from teatree.agents.harness_options import HarnessOptions
from teatree.agents.model_tiering import resolve_pydantic_ai_model
from teatree.agents.pydantic_ai_config import LANE_EVAL, OpenAICompatibleLaneConfig
from teatree.agents.pydantic_ai_session import PydanticAiHarnessSession
from teatree.agents.regulated_path import assert_model_allowed_on_regulated_path
from teatree.config import get_effective_settings
from teatree.config.settings import PYDANTIC_AI_MAX_TOKENS_DEFAULT
from teatree.eval.api_runner import load_agent_definition
from teatree.eval.message_mapping import eval_run_from_messages
from teatree.eval.model_resolution import resolve_spec_model
from teatree.eval.model_variant import parse_model_variant
from teatree.eval.models import EvalRun, EvalSpec
from teatree.eval.prompt_framing import LIVE_ENV_FRAMING
from teatree.eval.resource_caps import resolve_watchdog_seconds
from teatree.eval.under_load import build_system_prompt, build_user_prompt
from teatree.llm.openai_compatible import OpenAICompatibleCredential, resolve_openai_compatible_backend

#: The dispatch-lane header (mirrors ``teatree.agents.harness._X_LANE_HEADER``).
_X_LANE_HEADER = "x-lane"

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


def build_eval_toolset(tool_names: tuple[str, ...]) -> FunctionToolset[None]:
    """A ``pydantic_ai`` toolset of inert stubs, one per scenario-declared tool.

    Each of *tool_names* (``EvalSpec.tools``) becomes an arbitrary-argument stub so
    the model can issue the call the matchers grade without any real execution.
    """
    toolset: FunctionToolset[None] = FunctionToolset()
    for name in tool_names:
        toolset.add_function(_inert_tool, name=name)
    return toolset


def _system_prompt(spec: EvalSpec) -> str:
    """The clean-room system prompt: the agent definition + the live-env framing.

    Identical construction to the ``api`` lane (:mod:`teatree.eval.api_runner`) so a
    scenario grades the SAME agent definition regardless of the backend.
    """
    clean_room_prompt = load_agent_definition(spec.agent_path, spec.agent_sections) + LIVE_ENV_FRAMING
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
    a headless dispatch does, rather than handing the provider a level it rejects.

    Deliberately NOT routed through the harness lane's
    :func:`~teatree.agents.pydantic_ai_config.build_model_settings`: that builder branches
    on the harness's ``PydanticAiBinding`` enum rather than on a ``Model`` (the eval lane
    is handed injectable model doubles), maps efforts through
    ``ANTHROPIC_THINKING_EFFORT_MAP`` where this lane drops an out-of-vocabulary rung, and
    carries no cache key. Unifying them means changing the effort actually sent.
    """
    resolved = resolve_effort(HarnessOptions(effort=effort))
    if model.system == _ANTHROPIC_SYSTEM:
        return _anthropic_settings(resolved, max_tokens)
    return _openai_settings(resolved, max_tokens)


@dataclass(frozen=True, slots=True)
class EvalDriveCaps:
    """What bounds ONE eval drive, shared by both fresh-run lanes (composition).

    *   ``turn_cap`` — an explicit ``--max-turns``; ``None`` defers to the backend's
        per-run request-loop guardrail.
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
    ) -> None:
        self._model = model
        self._caps = caps or EvalDriveCaps()
        self._backend = backend or OpenAICompatibleLaneConfig(lane=LANE_EVAL)

    def run(self, spec: EvalSpec) -> EvalRun:
        # Resolve the abstract tier/phase to a concrete model id (a no-op when the
        # spec already carries a concrete ``model``); the resolved id flows into the
        # variant parse, the model-presence check, the ledger label, and the report.
        spec = resolve_spec_model(spec)
        model = self._resolve_model(spec)
        try:
            messages = asyncio.run(self._drive_with_watchdog(spec, model))
        except TimeoutError:
            return EvalRun.terminal(spec.name, terminal_reason="timeout")
        return eval_run_from_messages(spec, messages)

    def _resolve_model(self, spec: EvalSpec) -> Model:
        if self._model is not None:
            return self._model
        # Build the real backend model on the eval lane. The model normalisation and
        # the regulated-path allowlist gate are the shared PUBLIC functions the harness
        # uses; only the provider client (mirroring
        # ``teatree.agents.pydantic_ai_config.build_openai_compatible_provider``) is built
        # here so the eval runner never reaches into the harness's private surface.
        # Credential resolution is lazy (never at runner construction).
        pinned = parse_model_variant(spec.model).model
        resolved = resolve_pydantic_ai_model(pinned, configured_model=self._backend.model)
        assert_model_allowed_on_regulated_path(pinned or resolved)
        backend = resolve_openai_compatible_backend(
            base_url=self._backend.base_url,
            model=resolved,
            credential=OpenAICompatibleCredential(pass_path_override=self._backend.credential_entry or None),
        )
        client = AsyncOpenAI(
            base_url=backend.base_url, api_key=backend.api_key, default_headers={_X_LANE_HEADER: self._backend.lane}
        )
        return OpenAIChatModel(resolved, provider=OpenAIProvider(openai_client=client))

    async def _drive_with_watchdog(self, spec: EvalSpec, model: Model) -> list[Message]:
        return await asyncio.wait_for(self._drive(spec, model), timeout=resolve_watchdog_seconds())

    async def _drive(self, spec: EvalSpec, model: Model) -> list[Message]:
        variant = parse_model_variant(spec.model)
        effort = variant.effort if variant.effort is not None else self._caps.effort
        agent: Agent[None, str] = Agent(
            model,
            system_prompt=_system_prompt(spec),
            model_settings=_model_settings(model, effort, self._caps.max_tokens),
            toolsets=[build_eval_toolset(spec.tools)],
        )
        # An explicit ``--max-turns`` caps the request loop; else the backend
        # per-run guardrail; else uncapped (the watchdog is the hang backstop).
        request_limit = self._caps.turn_cap if self._caps.turn_cap is not None else self._backend.request_limit
        # ``async with agent`` enters the model so the provider's HTTP client closes
        # cleanly on exit rather than leaking one per run.
        async with agent:
            session = PydanticAiHarnessSession(agent, model_name=model.model_name, request_limit=request_limit)
            await session.query(build_user_prompt(spec))
            return [cast("Message", message) async for message in session.receive_response()]


def build_pydantic_ai_eval_runner(
    *,
    max_turns_override: int | None = None,
    effort: EffortLevel | None = None,
) -> PydanticAiRunner:
    """Build the ``pydantic_ai`` eval runner with the eval-lane backend knobs.

    The DB-home backend settings (the per-run step cap, the output-token ceiling, the
    endpoint, the model id, the credential-store entry) are resolved SYNCHRONOUSLY here
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
        ),
    )


__all__ = ["EvalDriveCaps", "PydanticAiRunner", "build_eval_toolset", "build_pydantic_ai_eval_runner"]
