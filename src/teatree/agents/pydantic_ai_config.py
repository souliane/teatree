"""Config + model/provider builders for the ``pydantic_ai`` harness backend.

Split out of :mod:`teatree.agents.harness` (module-health LOC cap): the pure config types
(the OpenAI-compatible backend knobs, the model-construction bundle, the binding, the
per-backend capability constants) and the two provider/model builders. They depend on neither
the ``Harness`` protocol nor the registry-resolution glue, so they live below the harness
module with no import cycle. Re-exported from ``teatree.agents.harness`` for back-compat.
"""

from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any, cast

from openai import AsyncOpenAI
from pydantic_ai.models import Model
from pydantic_ai.models.openai import ReasoningEffort
from pydantic_ai.profiles.anthropic import ANTHROPIC_THINKING_EFFORT_MAP, resolve_anthropic_effort
from pydantic_ai.providers.openai import OpenAIProvider
from pydantic_ai.settings import ModelSettings

from teatree.agents.harness_options import HarnessOptions
from teatree.agents.harness_registry import HarnessCapabilities
from teatree.agents.model_tiering import DEFAULT_TIER, resolve_tier
from teatree.agents.regulated_path import assert_model_allowed_on_regulated_path
from teatree.llm.credentials import AnthropicApiKeyCredential
from teatree.llm.openai_compatible import OpenAICompatibleCredential, resolve_openai_compatible_backend

# The dispatch-lane header. Rides every ``pydantic_ai`` request as
# ``x-lane: <factory|eval|bulk>`` so the endpoint's analytics — and any routing rule
# keying on it — can tell the three call-site lanes apart: the headless factory
# dispatch (``factory``), the eval CI job (``eval``), and a secondary overlay's cheap
# bulk legs (``bulk``). The value is the DB-home ``openai_compatible_lane`` setting,
# resolved SYNCHRONOUSLY in :func:`resolve_harness`.
_X_LANE_HEADER = "x-lane"
LANE_FACTORY = "factory"
LANE_EVAL = "eval"
LANE_BULK = "bulk"


class PydanticAiBinding(StrEnum):
    """Which model binding the ``pydantic_ai`` harness constructs (#3157 E1b).

    *   :attr:`ROUTER` (default) — an ``OpenAIChatModel`` against the configured
        OpenAI-compatible endpoint. Prompt-cache semantics are opaque behind that
        surface, so ``cache_control`` is unreachable.
    *   :attr:`NATIVE_ANTHROPIC` — a native ``pydantic_ai`` Anthropic model against the
        direct Messages API (``agent_harness_provider=anthropic_api``), where explicit
        ``cache_control`` breakpoints ARE reachable. The cheapest path to real caching
        without a third harness class — one branch in ``_resolve_model``.
    """

    ROUTER = "router"
    NATIVE_ANTHROPIC = "native_anthropic"


#: The OpenAI-compatible binding's capabilities: MCP toolsets; no hooks port yet,
#: no server-side resume (client-side thread reseed), no reachable cache-control (opaque router
#: surface), and no schema-enforced structured output (the lane scrapes the last JSON line of
#: agent text, it does not enforce a result schema). Dispatch-lane hints (#3157 AH-5): the
#: metered lane, no bundled CLI child (the credential resolves in-process).
PYDANTIC_AI_ROUTER_CAPABILITIES = HarnessCapabilities(
    hooks=False,
    mcp=True,
    cache_control=False,
    server_resume=False,
    structured_output=False,
    spawns_cli_child=False,
    metered_lane=True,
)
#: The native Anthropic Messages-API binding's capabilities (#3157 E1b): the router set
#: PLUS reachable ``cache_control`` breakpoints (still no schema-enforced structured output).
#: Same dispatch-lane hints (metered, no child).
PYDANTIC_AI_NATIVE_CAPABILITIES = HarnessCapabilities(
    hooks=False,
    mcp=True,
    cache_control=True,
    server_resume=False,
    structured_output=False,
    spawns_cli_child=False,
    metered_lane=True,
)


class NativeAnthropicUnavailableError(RuntimeError):
    """The native Anthropic binding was selected but ``pydantic-ai-slim[anthropic]`` is absent."""


@dataclass(frozen=True)
class OpenAICompatibleLaneConfig:
    """The generic OpenAI-compatible backend's per-dispatch knobs (#3666).

    Bundled into one cohesive config object (composition) so the harness
    constructor stays narrow, and — critically — so ALL of these DB-home settings
    are resolved SYNCHRONOUSLY by :func:`resolve_harness` before the async
    ``open`` runs (a ``get_effective_settings`` read from inside the ``asyncio.run``
    event loop fails safe to defaults under Django's async-unsafe guard).

    *   ``lane`` — the ``x-lane`` header (``factory`` | ``eval`` | ``bulk``).
    *   ``request_limit`` — the per-run sequential-request cap; ``None``/``<= 0``
        leaves the run uncapped.
    *   ``base_url`` — the ``openai_compatible_base_url`` endpoint. Empty falls back
        to the ``OPENAI_COMPATIBLE_BASE_URL`` env var, then fails loud: teatree never
        guesses an endpoint.
    *   ``credential_entry`` — the NAME of the credential-store entry the API key is
        read from (``openai_compatible_credential_entry``), never a key value.
        ``None`` means the credential resolves from ``OPENAI_COMPATIBLE_API_KEY``
        alone (or fails loud naming the setting).
    *   ``model`` — the ``openai_compatible_model`` id an unpinned teatree-native
        dispatch normalises UP to; ``None`` keeps the ``PYDANTIC_AI_TIER_MODELS``
        default for the dispatch's abstract tier.
    """

    lane: str = LANE_FACTORY
    request_limit: int | None = None
    base_url: str = ""
    credential_entry: str | None = None
    model: str | None = None


@dataclass(frozen=True, slots=True)
class PydanticAiModelConfig:
    """How :class:`PydanticAiHarness` builds its model + session (composition).

    Bundles the model-construction knobs into one config object so the harness
    constructor stays narrow:

    *   ``backend`` — the generic OpenAI-compatible backend's per-dispatch knobs
        (:class:`OpenAICompatibleLaneConfig`), used by the router binding.
    *   ``binding`` — the generic OpenAI-compatible backend vs native Anthropic Messages
        API (#3157 E1b, ``agent_harness_provider=anthropic_api``), selected by
        :func:`resolve_harness` from the provider.
    *   ``max_tokens`` — the per-request output-token ceiling. Binding-AGNOSTIC (``max_tokens``
        is a base ``ModelSettings`` key both bindings honour), so it lives here rather than on
        the router-only :class:`OpenAICompatibleLaneConfig`. Resolved SYNCHRONOUSLY by :func:`resolve_harness`
        from the ``pydantic_ai_max_tokens`` setting; ``None`` leaves the binding's own default.

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

    backend: OpenAICompatibleLaneConfig = field(default_factory=OpenAICompatibleLaneConfig)
    binding: PydanticAiBinding = PydanticAiBinding.ROUTER
    max_tokens: int | None = None


def native_anthropic_model_name(options: HarnessOptions) -> str:
    """The Anthropic Messages-API model id for the native binding (#3157 AH-4).

    An explicit ``options.model`` passes through unchanged (a Claude dash-form id is a valid
    Anthropic Messages-API model). An UNPINNED dispatch falls back to the default tier's
    CONCRETE Claude id (:func:`resolve_tier`, e.g. ``claude-sonnet-5``).

    Critically it must NOT go through :func:`~teatree.agents.model_tiering.resolve_pydantic_ai_model`,
    which normalises an unpinned id UP to the configured OpenAI-compatible model id. That id is
    meaningless to the direct Anthropic Messages API (it would 404 the request) — the normalise-UP
    step belongs ONLY to the OpenAI-compatible binding, never this native path.
    """
    return options.model or resolve_tier(DEFAULT_TIER)


def resolve_native_anthropic_model(options: HarnessOptions) -> Model:
    """Construct the direct Anthropic Messages-API model (#3157 E1b) — the cache_control path.

    The one branch that makes real ``cache_control`` reachable: a native ``pydantic_ai``
    Anthropic model authenticated with the metered Anthropic API key, instead of the
    OpenAI-compatible router client. ``pydantic_ai``'s Anthropic binding is an OPTIONAL
    dependency (``pydantic-ai-slim[anthropic]``); it is imported lazily so a router-only
    install never pays for it, and its absence fails LOUD with the install hint only when
    this binding is actually selected — the same late-fail contract the OpenAI-compatible
    credential uses. The model id (:func:`native_anthropic_model_name`) is gated by the regulated-path
    allowlist first.
    """
    model_name = native_anthropic_model_name(options)
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


def build_model_settings(
    model: Model, effort: ReasoningEffort | None, *, binding: PydanticAiBinding, max_tokens: int | None
) -> ModelSettings | None:
    """The model settings for *binding*: the base ``max_tokens`` ceiling plus the reasoning effort.

    ``max_tokens`` is a base :class:`~pydantic_ai.settings.ModelSettings` key both bindings
    honour on the wire (a 4096 default truncates a long result envelope mid-JSON), so it is
    merged in unconditionally when set. The reasoning effort is the binding-specific part.

    The settings namespace is per-provider in pydantic_ai, so the effort key is
    binding-specific and NOT interchangeable: an
    :class:`~pydantic_ai.models.openai.OpenAIChatModel` reads
    ``openai_reasoning_effort`` while an
    :class:`~pydantic_ai.models.anthropic.AnthropicModel` reads ``anthropic_effort``
    and ignores every foreign key silently. Handing the OpenAI-shaped settings to the
    native Anthropic binding therefore dropped the resolved effort with no error — the
    whole effort axis was a no-op on that lane.

    The vocabularies differ too. ``HARNESS_EFFORT_SCALE[pydantic_ai]`` is the
    OpenAI/router scale (it carries ``minimal``), while the Anthropic Messages API
    accepts only ``low``/``medium``/``high``/``xhigh``/``max``. An explicitly-set
    ``anthropic_effort`` is passed STRAIGHT to the wire by pydantic_ai (no profile
    gate, no mapping), so ``minimal`` would 400. The translation therefore goes
    through pydantic_ai's own canonical
    :data:`~pydantic_ai.profiles.anthropic.ANTHROPIC_THINKING_EFFORT_MAP` via
    :func:`~pydantic_ai.profiles.anthropic.resolve_anthropic_effort`, which also owns
    the per-model ``xhigh`` passthrough decision — never a vocabulary re-invented here.
    """
    # Built as a plain mapping rather than the per-binding ``…ModelSettings`` TypedDicts: the
    # ``AnthropicModelSettings`` symbol lives in ``pydantic_ai.models.anthropic``, which imports
    # the OPTIONAL ``anthropic`` extra — naming it here (even lazily) would couple this branch to
    # an extra a router-only install does not carry, while the runtime shape is identical (a
    # TypedDict IS a dict).
    settings: dict[str, Any] = {}
    if max_tokens is not None and max_tokens > 0:
        settings["max_tokens"] = max_tokens
    if effort is not None:
        if binding is PydanticAiBinding.NATIVE_ANTHROPIC:
            if effort in ANTHROPIC_THINKING_EFFORT_MAP:
                settings["anthropic_effort"] = resolve_anthropic_effort(
                    effort,
                    supports_xhigh=model.profile.get("anthropic_supports_xhigh_effort", False),
                )
        else:
            settings["openai_reasoning_effort"] = effort
    return cast("ModelSettings", settings) if settings else None


def build_openai_compatible_provider(config: OpenAICompatibleLaneConfig) -> OpenAIProvider:
    """Build the configured OpenAI-compatible provider with the ``x-lane`` header.

    Every provider-specific fact — the endpoint, the credential-store entry name —
    arrives as ordinary configuration on *config*, resolved SYNCHRONOUSLY by
    :func:`resolve_harness` (never here: this runs in the async event loop). The
    provider is built from an :class:`~openai.AsyncOpenAI` client carrying a default
    ``x-lane: <lane>`` header on every request — the only way to inject a default
    header, since :class:`OpenAIProvider` sets none itself.
    """
    backend = resolve_openai_compatible_backend(
        base_url=config.base_url,
        model=config.model or "",
        credential=OpenAICompatibleCredential(pass_path_override=config.credential_entry or None),
    )
    client = AsyncOpenAI(
        base_url=backend.base_url, api_key=backend.api_key, default_headers={_X_LANE_HEADER: config.lane}
    )
    return OpenAIProvider(openai_client=client)
