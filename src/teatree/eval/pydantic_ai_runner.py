"""Non-Claude eval execution over the provider-agnostic ``pydantic_ai`` harness seam.

The third :class:`~teatree.eval.backends.EvalRunner`, and the model-evolution
unblock. Where the ``api`` backend runs the Claude CLI via ``claude-agent-sdk`` and
``transcript`` replays recorded Claude Code JSONL, this backend drives a
``pydantic_ai`` :class:`~pydantic_ai.Agent` (OrcaRouter BYOK, OpenAI-compatible) so
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
import dataclasses
from typing import cast

from claude_agent_sdk import Message
from claude_agent_sdk.types import EffortLevel
from openai import AsyncOpenAI
from pydantic_ai import Agent
from pydantic_ai.models import Model
from pydantic_ai.models.openai import OpenAIChatModel, OpenAIChatModelSettings
from pydantic_ai.providers.openai import OpenAIProvider
from pydantic_ai.settings import ModelSettings
from pydantic_ai.toolsets import FunctionToolset

from teatree.agents.harness import resolve_effort
from teatree.agents.harness_options import HarnessOptions
from teatree.agents.model_tiering import resolve_pydantic_ai_model
from teatree.agents.pydantic_ai_config import LANE_EVAL, OrcaLaneConfig
from teatree.agents.pydantic_ai_session import PydanticAiHarnessSession
from teatree.agents.regulated_path import assert_model_allowed_on_regulated_path
from teatree.config import get_effective_settings
from teatree.eval.api_runner import load_agent_definition, resolve_watchdog_seconds
from teatree.eval.message_mapping import eval_run_from_messages
from teatree.eval.model_resolution import resolve_eval_model
from teatree.eval.model_variant import parse_model_variant
from teatree.eval.models import EvalRun, EvalSpec
from teatree.eval.prompt_framing import LIVE_ENV_FRAMING
from teatree.eval.under_load import build_system_prompt, build_user_prompt
from teatree.llm.credentials import OrcaRouterCredential, resolve_orca_router_provider_config

#: The OrcaRouter dispatch-lane header (mirrors ``teatree.agents.harness._X_LANE_HEADER``).
_X_LANE_HEADER = "x-lane"


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


def _model_settings(effort: EffortLevel | None) -> ModelSettings | None:
    """Map a resolved reasoning effort to OpenAI-compatible model settings, or ``None``.

    Reuses the harness's effort-vocabulary guard (:func:`~teatree.agents.harness.resolve_effort`)
    so the ``pydantic_ai`` lane drops an out-of-vocabulary rung (``max``) exactly as
    a headless dispatch does, rather than handing the provider a level it rejects.
    """
    resolved = resolve_effort(HarnessOptions(effort=effort))
    if resolved is None:
        return None
    return OpenAIChatModelSettings(openai_reasoning_effort=resolved)


class PydanticAiRunner:
    """Run an :class:`EvalSpec` through the ``pydantic_ai`` harness — the non-Claude lane.

    *model* is INJECTABLE (default ``None`` resolves the real OrcaRouter model lazily
    inside :meth:`run`, so building the runner never needs a live credential): a test
    drives it with pydantic_ai's own :class:`~pydantic_ai.models.test.TestModel` /
    :class:`~pydantic_ai.models.function.FunctionModel` doubles, no network, no token.
    """

    def __init__(
        self,
        *,
        model: Model | None = None,
        max_turns_override: int | None = None,
        effort: EffortLevel | None = None,
        orca: OrcaLaneConfig | None = None,
    ) -> None:
        self._model = model
        self._max_turns_override = max_turns_override
        #: Lane-level representative reasoning effort applied when a scenario declares
        #: no ``model@effort`` of its own (a declared effort wins).
        self._effort = effort
        self._orca = orca or OrcaLaneConfig(lane=LANE_EVAL)

    def run(self, spec: EvalSpec) -> EvalRun:
        # Resolve the abstract tier/phase to a concrete model id (a no-op when the
        # spec already carries a concrete ``model``); the resolved id flows into the
        # variant parse, the model-presence check, the ledger label, and the report.
        spec = dataclasses.replace(spec, model=resolve_eval_model(spec))
        model = self._resolve_model(spec)
        try:
            messages = asyncio.run(self._drive_with_watchdog(spec, model))
        except TimeoutError:
            return _terminal_eval_run(spec, terminal_reason="timeout")
        return eval_run_from_messages(spec, messages)

    def _resolve_model(self, spec: EvalSpec) -> Model:
        if self._model is not None:
            return self._model
        # Build the real OrcaRouter model on the eval lane. The abstract-tier→router
        # handle normalisation and the regulated-path allowlist gate are the shared
        # PUBLIC functions the harness uses; only the provider client (mirroring
        # ``teatree.agents.harness._build_orca_provider``) is built here so the eval
        # runner never reaches into the harness's private surface. Credential
        # resolution is lazy (never at runner construction).
        pinned = parse_model_variant(spec.model).model
        resolved = resolve_pydantic_ai_model(pinned, router_name=self._orca.router_name)
        assert_model_allowed_on_regulated_path(pinned or resolved)
        config = resolve_orca_router_provider_config(
            credential=OrcaRouterCredential(pass_path_override=self._orca.pass_path or None)
        )
        client = AsyncOpenAI(
            base_url=config.base_url, api_key=config.api_key, default_headers={_X_LANE_HEADER: self._orca.lane}
        )
        return OpenAIChatModel(resolved, provider=OpenAIProvider(openai_client=client))

    async def _drive_with_watchdog(self, spec: EvalSpec, model: Model) -> list[Message]:
        return await asyncio.wait_for(self._drive(spec, model), timeout=resolve_watchdog_seconds())

    async def _drive(self, spec: EvalSpec, model: Model) -> list[Message]:
        variant = parse_model_variant(spec.model)
        effort = variant.effort if variant.effort is not None else self._effort
        agent: Agent[None, str] = Agent(
            model,
            system_prompt=_system_prompt(spec),
            model_settings=_model_settings(effort),
            toolsets=[build_eval_toolset(spec.tools)],
        )
        # An explicit ``--max-turns`` caps the request loop; else the OrcaRouter
        # per-run guardrail; else uncapped (the watchdog is the hang backstop).
        request_limit = self._max_turns_override if self._max_turns_override is not None else self._orca.request_limit
        # ``async with agent`` enters the model so the provider's HTTP client closes
        # cleanly on exit rather than leaking one per run.
        async with agent:
            session = PydanticAiHarnessSession(agent, model_name=model.model_name, request_limit=request_limit)
            await session.query(build_user_prompt(spec))
            return [cast("Message", message) async for message in session.receive_response()]


def _terminal_eval_run(spec: EvalSpec, *, terminal_reason: str) -> EvalRun:
    """An error-shaped run for a lane that produced no transcript (the watchdog fired)."""
    return EvalRun(
        spec_name=spec.name,
        tool_calls=(),
        text_blocks=(),
        terminal_reason=terminal_reason,
        is_error=True,
        raw_stdout="",
        raw_stderr="",
    )


def build_pydantic_ai_eval_runner(
    *,
    max_turns_override: int | None = None,
    effort: EffortLevel | None = None,
) -> PydanticAiRunner:
    """Build the ``pydantic_ai`` eval runner with the eval-lane OrcaRouter knobs.

    The DB-home OrcaRouter settings (the per-run step cap, the pass-path override,
    the per-overlay router handle) are resolved SYNCHRONOUSLY here — never inside the
    async ``run``, where a ``get_effective_settings`` read fails safe to defaults
    under Django's async guard — and pinned to the ``eval`` dispatch lane
    (``x-lane: eval``). This mirrors :func:`teatree.agents.harness.resolve_harness`.
    """
    settings = get_effective_settings()
    return PydanticAiRunner(
        max_turns_override=max_turns_override,
        effort=effort,
        orca=OrcaLaneConfig(
            lane=LANE_EVAL,
            request_limit=settings.pydantic_ai_request_limit,
            pass_path=settings.orca_router_pass_path or None,
            router_name=settings.orca_router_name or None,
        ),
    )


__all__ = ["PydanticAiRunner", "build_eval_toolset", "build_pydantic_ai_eval_runner"]
