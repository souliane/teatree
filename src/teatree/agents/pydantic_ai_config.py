"""Config + model/provider builders for the ``pydantic_ai`` harness backend.

Split out of :mod:`teatree.agents.harness` (module-health LOC cap): the pure config types
(the OrcaRouter lane knobs, the model-construction bundle, the router-vs-native binding, the
per-backend capability constants) and the two provider/model builders. They depend on neither
the ``Harness`` protocol nor the registry-resolution glue, so they live below the harness
module with no import cycle. Re-exported from ``teatree.agents.harness`` for back-compat.
"""

from dataclasses import dataclass, field
from enum import StrEnum

from claude_agent_sdk import ClaudeAgentOptions
from openai import AsyncOpenAI
from pydantic_ai.models import Model
from pydantic_ai.providers.openai import OpenAIProvider

from teatree.agents.harness_registry import HarnessCapabilities
from teatree.agents.model_tiering import resolve_pydantic_ai_model
from teatree.agents.regulated_path import assert_model_allowed_on_regulated_path
from teatree.llm.credentials import AnthropicApiKeyCredential, OrcaRouterCredential, resolve_orca_router_provider_config

# The OrcaRouter dispatch-lane header (OrcaRouter setup plan §3.4). Rides every
# ``pydantic_ai`` request as ``x-lane: <factory|eval|bulk>`` so the named router's
# analytics — and a DSL rule that keys on it (a secondary router's ``headers["x-lane"]
# == "bulk"`` cheap-bulk rule) — can tell the three call-site lanes apart: the
# headless factory dispatch (``factory``), the eval CI job (``eval``), and a
# secondary overlay's cheap bulk legs (``bulk``). The value is the DB-home
# ``orca_router_lane`` setting, resolved SYNCHRONOUSLY in :func:`resolve_harness`.
_X_LANE_HEADER = "x-lane"
LANE_FACTORY = "factory"
LANE_EVAL = "eval"
LANE_BULK = "bulk"


class PydanticAiBinding(StrEnum):
    """Which model binding the ``pydantic_ai`` harness constructs (#3157 E1b).

    *   :attr:`ROUTER` (default) — an ``OpenAIChatModel`` against OrcaRouter's
        OpenAI-compatible endpoint. Prompt-cache semantics are opaque behind that
        surface, so ``cache_control`` is unreachable.
    *   :attr:`NATIVE_ANTHROPIC` — a native ``pydantic_ai`` Anthropic model against the
        direct Messages API (``agent_harness_provider=anthropic_api``), where explicit
        ``cache_control`` breakpoints ARE reachable. The cheapest path to real caching
        without a third harness class — one branch in ``_resolve_model``.
    """

    ROUTER = "router"
    NATIVE_ANTHROPIC = "native_anthropic"


#: The OpenAI-compatible OrcaRouter binding's capabilities: MCP toolsets + schema-enforced
#: structured output; no hooks port yet, no server-side resume (client-side thread reseed),
#: no reachable cache-control (opaque router surface).
PYDANTIC_AI_ROUTER_CAPABILITIES = HarnessCapabilities(
    hooks=False, mcp=True, cache_control=False, server_resume=False, structured_output=True
)
#: The native Anthropic Messages-API binding's capabilities (#3157 E1b): the router set
#: PLUS reachable ``cache_control`` breakpoints.
PYDANTIC_AI_NATIVE_CAPABILITIES = HarnessCapabilities(
    hooks=False, mcp=True, cache_control=True, server_resume=False, structured_output=True
)


class NativeAnthropicUnavailableError(RuntimeError):
    """The native Anthropic binding was selected but ``pydantic-ai-slim[anthropic]`` is absent."""


@dataclass(frozen=True)
class OrcaLaneConfig:
    """The OrcaRouter per-dispatch runtime knobs threaded into :class:`PydanticAiHarness`.

    Bundled into one cohesive config object (composition) so the harness
    constructor stays narrow, and — critically — so ALL of these DB-home settings
    are resolved SYNCHRONOUSLY by :func:`resolve_harness` before the async
    ``open`` runs (a ``get_effective_settings`` read from inside the ``asyncio.run``
    event loop fails safe to defaults under Django's async-unsafe guard).

    *   ``lane`` — the ``x-lane`` header (``factory`` | ``eval`` | ``bulk``, plan §3.4).
    *   ``request_limit`` — the per-run sequential-request cap (plan §4 #1);
        ``None``/``<= 0`` leaves the run uncapped.
    *   ``pass_path`` — the ``orca_router_pass_path`` override (plan §3.6). The
        credential has NO built-in default, so ``None`` means it resolves only from
        ``ORCA_ROUTER_API_KEY`` (or fails loud naming ``orca_router_pass_path``).
    *   ``router_name`` — the per-overlay OrcaRouter router handle
        (``orca_router_name``, e.g. ``orcarouter/secondary-factory``) the ``teatree-native``
        model id normalises UP to; ``None`` keeps the ``PYDANTIC_AI_TIER_MODELS``
        default (``orcarouter/teatree-factory``). The config/overlay-driven
        ``teatree-factory`` vs secondary-router selection.
    """

    lane: str = LANE_FACTORY
    request_limit: int | None = None
    pass_path: str | None = None
    router_name: str | None = None


@dataclass(frozen=True, slots=True)
class PydanticAiModelConfig:
    """How :class:`PydanticAiHarness` builds its model + session (composition).

    Bundles the model-construction knobs into one config object so the harness
    constructor stays narrow:

    *   ``orca`` — the OrcaRouter per-dispatch runtime knobs (:class:`OrcaLaneConfig`),
        used by the router binding.
    *   ``binding`` — router (OrcaRouter OpenAI-compatible) vs native Anthropic Messages
        API (#3157 E1b, ``agent_harness_provider=anthropic_api``), selected by
        :func:`resolve_harness` from the provider.

    Prompt-cache breakpoints (the :class:`~teatree.agents.context_plan.ContextPlan`
    → ``cache_control`` path, #3157 E2) are NOT wired into the harness here: nothing
    yet assembles a ``ContextPlan`` on the dispatch path, and applying its breakpoints
    to a live request needs the native-binding option-builder to emit pydantic_ai
    :class:`~pydantic_ai.messages.CachePoint` markers — deferred to the Factory
    overlay's option-builder. The ``ContextPlan`` core type (byte-stable-head
    enforcement + breakpoint capping) ships and is proven through the
    ``teatree.overlay_sdk`` surface (``test_factory_demo``) so the Factory can build
    against it; only the never-fed harness passthrough was removed (AH-1).
    """

    orca: OrcaLaneConfig = field(default_factory=OrcaLaneConfig)
    binding: PydanticAiBinding = PydanticAiBinding.ROUTER


def resolve_native_anthropic_model(options: ClaudeAgentOptions) -> Model:
    """Construct the direct Anthropic Messages-API model (#3157 E1b) — the cache_control path.

    The one branch that makes real ``cache_control`` reachable: a native ``pydantic_ai``
    Anthropic model authenticated with the metered Anthropic API key, instead of the
    OpenAI-compatible router client. ``pydantic_ai``'s Anthropic binding is an OPTIONAL
    dependency (``pydantic-ai-slim[anthropic]``); it is imported lazily so a router-only
    install never pays for it, and its absence fails LOUD with the install hint only when
    this binding is actually selected — the same late-fail contract the OrcaRouter credential
    uses. The model id passes through unchanged (a Claude dash-form id is a valid Anthropic
    Messages-API model), gated by the regulated-path allowlist first.
    """
    model_name = options.model or resolve_pydantic_ai_model(options.model)
    assert_model_allowed_on_regulated_path(model_name)
    try:
        from pydantic_ai.models.anthropic import AnthropicModel  # noqa: PLC0415 — optional extra, imported lazily
        from pydantic_ai.providers.anthropic import AnthropicProvider  # noqa: PLC0415 — optional extra, imported lazily
    except ImportError as exc:
        msg = (
            "The native Anthropic binding (agent_harness_provider=anthropic_api) needs the "
            "'anthropic' package — install pydantic-ai-slim[anthropic]."
        )
        raise NativeAnthropicUnavailableError(msg) from exc
    api_key = AnthropicApiKeyCredential().resolve()
    return AnthropicModel(model_name, provider=AnthropicProvider(api_key=api_key))


def build_orca_provider(*, lane: str, pass_path: str | None = None) -> OpenAIProvider:
    """Build the OrcaRouter OpenAI-compatible provider with the ``x-lane`` header (§3.4).

    Resolves the BYOK credential + base_url
    (:func:`~teatree.llm.credentials.resolve_orca_router_provider_config`).
    *pass_path* is the DB-home ``orca_router_pass_path`` override (resolved
    SYNCHRONOUSLY by :func:`resolve_harness`, never here — this runs in the async
    event loop), so an operator can point teatree at an existing per-account
    ``pass`` entry with no copy. The credential has NO built-in default, so
    ``None``/empty means it resolves only from the ``ORCA_ROUTER_API_KEY`` env var
    (which still wins over ``pass``) and otherwise fails loud. The
    provider is built from an :class:`~openai.AsyncOpenAI` client carrying a
    default ``x-lane: <lane>`` header on every request — the only way to inject a
    default header, since :class:`OpenAIProvider` sets none itself.
    """
    config = resolve_orca_router_provider_config(credential=OrcaRouterCredential(pass_path_override=pass_path or None))
    client = AsyncOpenAI(base_url=config.base_url, api_key=config.api_key, default_headers={_X_LANE_HEADER: lane})
    return OpenAIProvider(openai_client=client)
