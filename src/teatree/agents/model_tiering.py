"""Per-phase headless model tiering by ABSTRACT TIER (#880, #562 §3).

Every concrete Claude model id in teatree lives in EXACTLY ONE place: the
:data:`TIER_MODELS` constant below (the ``claude_sdk`` catalog; the
``pydantic_ai``/OrcaRouter harness has its own :data:`PYDANTIC_AI_TIER_MODELS`
catalog — see the harness-scoped note below). Everything else — production phase
dispatch, eval scenarios, the benchmark, and the tests — references an abstract
TIER (``frontier`` / ``balanced`` / ``cheap``), never a concrete model id.
Adopting a new model is one edit to :data:`TIER_MODELS` (or one
``[agent.tier_models]`` TOML line), with zero scenario, test, or dispatch edits.

The three tiers map to the price points #562 reasons about: ``frontier`` is the
full-reasoning tier (genuine design work), ``balanced`` the mid tier, ``cheap``
the mechanical tier. :func:`resolve_tier` reads :data:`TIER_MODELS` (overridable
via ``[agent.tier_models]``); :data:`DEFAULT_PHASE_MODELS` maps each FSM phase to
a tier, and :func:`resolve_phase_model` / :func:`resolve_spawn_model` resolve
phase → tier → concrete model id.

The reasoning-effort dial is the parallel single-source constant
:data:`TIER_EFFORT` (abstract tier → effort), read via
:func:`resolve_tier_effort`; :func:`resolve_spawn_effort` resolves phase → tier →
effort exactly as :func:`resolve_phase_model` resolves phase → tier → model
(same ``phase_models`` override mechanism). Only the reasoning tiers carry an
effort by default — ``cheap`` (Haiku, which rejects the lever) is absent, so its
spawns emit no effort and inherit the SDK default.

**Harness-scoped model + effort ([#2885](https://github.com/souliane/teatree/issues/2885)).**
:data:`TIER_MODELS` is the ``claude_sdk`` catalog — Claude ids in DASH-form
(``claude-opus-4-8``). The ``pydantic_ai`` harness's OrcaRouter provider does NOT
carry those dash-form ids (Orca serves provider-prefixed ids —
``anthropic/claude-opus-4.8``, the CN pool, and named router handles), so its
catalog is the SEPARATE :data:`PYDANTIC_AI_TIER_MODELS` (all tiers collapsing to
one router handle; the router's own bandit does the mundane-vs-hard tiering).
:func:`resolve_pydantic_ai_model` is the boundary that normalises a resolved
teatree-native id UP to that router handle for the OrcaRouter harness, while an
explicit Orca-native pin passes through. The reasoning-EFFORT dial is
similarly harness-scoped: the two harnesses' supported effort vocabularies are not identical —
the ``claude-agent-sdk`` CLI's scale tops out at ``max``
(:data:`teatree.config_agent.EFFORT_SCALE`), while ``pydantic_ai``'s OpenAI-compatible
``ReasoningEffort`` tops out at ``xhigh`` (no ``max``) — so :func:`resolve_tier_effort`
/ :func:`resolve_spawn_effort` validate their resolved value against the ACTIVE
harness's :data:`HARNESS_EFFORT_SCALE` entry and drop an out-of-range value (falling
back to the shipped default) rather than ever handing a harness an effort string it
does not understand. The shipped :data:`TIER_EFFORT` values (``xhigh`` / ``high``)
are valid on both scales today, so this is a no-op for the shipped defaults; it only
narrows an operator's ``[agent.tier_effort]`` override.

The mapping is config-driven via ``~/.teatree.toml``::

    [agent]
    phase_models.reviewing = "frontier"  # pin a phase to a tier (or a model id)
    phase_models.coding = "balanced"     # opt a phase into a cheaper tier
    phase_models.testing = ""            # opt out — inherit the user's default

    [agent.tier_models]
    frontier = "claude-opus-4-9"         # adopt a new frontier model, one line

    [agent.tier_effort]
    balanced = "xhigh"                   # raise the balanced-tier effort, one line

A ``phase_models`` override may name a TIER (resolved through
:func:`resolve_tier`) or a concrete model id (passed through unchanged), so a
power user can still pin a specific model. A sentinel value (empty / ``default``
/ ``inherit``) returns ``None`` so no ``--model`` flag is added and the user's
configured default applies unchanged.
"""

import tomllib
from collections.abc import Iterable
from pathlib import Path

from teatree.config import CONFIG_PATH, AgentHarness, get_effective_settings
from teatree.config_agent import _INHERIT_SENTINELS, EFFORT_SCALE, resolve_agent_config
from teatree.core.cost import tier_of_model, tier_rank

# THE SINGLE SOURCE OF TRUTH for concrete model ids. This is the ONLY place a
# concrete Claude model id appears in teatree's model-resolution code: abstract
# tier name -> concrete model id. Overridable per tier via ``[agent.tier_models]``
# (merged OVER this default), so adopting a new model is one edit here or one
# config line — no scenario, test, or dispatch edit.
TIER_MODELS: dict[str, str] = {
    "frontier": "claude-opus-4-8",
    "balanced": "claude-sonnet-5",
    "cheap": "claude-haiku-4-5",
}

# The ``pydantic_ai``/OrcaRouter parallel of :data:`TIER_MODELS`. The
# ``claude_sdk`` harness serves the Claude dash-form ids above; the
# ``pydantic_ai`` harness (:class:`~teatree.agents.harness.PydanticAiHarness`)
# serves OrcaRouter's provider-prefixed catalog, so its tier map is SEPARATE —
# :data:`TIER_MODELS`'s dash-form ids (``claude-opus-4-8``) do NOT exist in
# OrcaRouter's catalog (Orca carries ``anthropic/claude-opus-4.8`` dot-form and
# provider-prefixed CN ids), so trusting them here would send an unresolvable id.
# All three abstract tiers collapse to ONE router handle by design: the router's
# own adaptive/gated bandit does the mundane-vs-hard tiering, so teatree's
# abstract tiers need not fan out on this harness. Overridable per tier via
# ``[agent.pydantic_ai_tier_models]`` (merged OVER this default) — if the
# dashboard keeps a different router name, that is the one string to change.
PYDANTIC_AI_TIER_MODELS: dict[str, str] = {
    "frontier": "orcarouter/teatree-factory",
    "balanced": "orcarouter/teatree-factory",
    "cheap": "orcarouter/teatree-factory",
}

# Reverse of the abstract-tier → Claude-family relationship, for normalising a
# resolved Claude id back to its abstract tier when picking the ``pydantic_ai``
# router handle (:func:`resolve_pydantic_ai_model`). The pricing-side sibling is
# ``teatree.core.cost._FAMILY_TO_TIER``; this copy lives here because
# ``model_tiering`` OWNS the abstract tiers.
_CLAUDE_FAMILY_TO_TIER: dict[str, str] = {"opus": "frontier", "sonnet": "balanced", "haiku": "cheap"}

# THE SINGLE SOURCE OF TRUTH for per-tier reasoning EFFORT — the parallel of
# :data:`TIER_MODELS` for the effort axis. Abstract tier name -> CLI effort level
# (a member of :data:`teatree.config_agent.EFFORT_SCALE`). Overridable per tier
# via ``[agent.tier_effort]`` (merged OVER this default). Only the reasoning tiers
# carry an effort: ``cheap`` (Haiku, which rejects the effort/thinking levers) is
# deliberately ABSENT, so :func:`resolve_tier_effort` returns ``None`` for it and
# its spawns inherit the SDK default effort (emit no ``--effort``).
TIER_EFFORT: dict[str, str] = {
    "frontier": "xhigh",
    "balanced": "high",
}

# HARNESS-SCOPED effort vocabularies ([#2885](https://github.com/souliane/teatree/issues/2885)):
# the set of effort strings each headless harness (:mod:`teatree.agents.harness`)
# actually understands. :func:`resolve_tier_effort` / :func:`resolve_spawn_effort`
# drop a resolved value that is not a member of the ACTIVE harness's set (falling
# back to the shipped :data:`TIER_EFFORT` default) rather than ever handing a
# harness an effort string outside its own scale.
#
# ``claude_sdk`` -> :data:`teatree.config_agent.EFFORT_SCALE` (the
# ``claude-agent-sdk`` CLI's own scale, unchanged — the config-time validator in
# ``config_agent.py`` already gates ``[agent.tier_effort]`` overrides against this
# same set). ``pydantic_ai`` -> the OpenAI-compatible ``ReasoningEffort`` /
# ``ThinkingLevel`` vocabulary pydantic_ai exposes (``minimal`` instead of
# ``claude_sdk``'s absent floor rung, no ``max`` ceiling rung).
HARNESS_EFFORT_SCALE: dict[AgentHarness, frozenset[str]] = {
    AgentHarness.CLAUDE_SDK: EFFORT_SCALE,
    AgentHarness.PYDANTIC_AI: frozenset({"minimal", "low", "medium", "high", "xhigh"}),
}

# The default tier for a phase NOT in :data:`DEFAULT_PHASE_MODELS`, and the
# default tier for an eval scenario that declares neither ``model:`` nor ``tier:``
# nor ``phase:``. The conservative mid tier.
DEFAULT_TIER = "balanced"

# The :func:`teatree.core.cost.tier_of_model` tier key whose models do NOT accept
# an adaptive-thinking pin: Haiku (the ``cheap`` tier) rejects the ``thinking`` /
# ``effort`` reasoning levers the Opus/Sonnet tiers accept. Matched on the
# tier so any future dated Haiku id is covered (:func:`model_supports_thinking`).
_NON_THINKING_TIER = "haiku"

# Default phase -> abstract TIER mapping. The genuine-reasoning phases
# (planning, coding, debugging, reviewing, retrospecting) get ``frontier``; the
# mechanical-but-non-trivial phases (testing, shipping) get ``balanced``; the
# pure-handoff phase (requesting_review) gets ``cheap``. A phase NOT in this dict
# resolves to :data:`DEFAULT_TIER`.
DEFAULT_PHASE_MODELS: dict[str, str] = {
    "planning": "frontier",
    "coding": "frontier",
    "debugging": "frontier",
    "reviewing": "frontier",
    "retrospecting": "frontier",
    "testing": "balanced",
    "shipping": "balanced",
    "requesting_review": "cheap",
}

# The phases a situational honesty-critical escalation routes to the most-honest
# model (teatree#2263). These are the *verification* phases — the sub-agent that
# produces a rubric PASS/FAIL or otherwise verifies the work. This is
# SITUATIONAL (gated on an active escalation row), NOT a phase floor:
# ``DEFAULT_PHASE_MODELS`` keeps each verification phase at its own tier so
# without an active escalation these phases resolve exactly as today.
VERIFICATION_PHASES: frozenset[str] = frozenset({"reviewing", "requesting_review", "testing"})

# Phases PINNED to a specific harness regardless of the overlay's ``agent_harness``
# setting — the CN-model verifier backstop (OrcaRouter setup plan §4 guardrail #2).
# When a MAKER phase runs on a cheap CN model via ``pydantic_ai``/OrcaRouter, the
# VERIFICATION phases (the checker in the maker≠checker pipeline) stay on the
# trusted ``claude_sdk`` lane, so the reliability backstop is a Claude verifier +
# CI, never the cheap maker model checking its own work. Data-driven so the pinned
# set is one place, not a branch in :func:`resolve_harness`.
PHASE_HARNESS: dict[str, AgentHarness] = dict.fromkeys(VERIFICATION_PHASES, AgentHarness.CLAUDE_SDK)


def resolve_phase_harness(configured: AgentHarness, phase: str | None) -> AgentHarness:
    """The harness a *phase* dispatch actually uses — *configured*, unless the phase is pinned.

    A phase in :data:`PHASE_HARNESS` (the verification phases) forces its pinned
    harness (``claude_sdk``) even when the overlay configured ``pydantic_ai`` — so
    the checker stays on the trusted lane while the maker rides the cheap one. Every
    other phase (and an absent *phase*) uses *configured* unchanged.
    """
    if phase is not None and phase in PHASE_HARNESS:
        return PHASE_HARNESS[phase]
    return configured


# The Chinese-models allowlist (#2887): substrings identifying a Chinese-origin
# model family reachable through OrcaRouter's routing handle. Matched
# case-insensitively against a resolved
# OrcaRouter model name in :func:`assert_chinese_model_allowed`. No shipped
# :data:`TIER_MODELS` entry matches today (every tier is a Claude model), so the
# check is currently a no-op for the shipped defaults — it exists for the day a
# tier (or a ``phase_models`` / ``tier_models`` override) points at one of these.
_CHINESE_MODEL_MARKERS: frozenset[str] = frozenset({"deepseek", "qwen", "glm"})


def is_chinese_origin_model(model_id: str) -> bool:
    """Whether *model_id* names a Chinese-origin model family (:data:`_CHINESE_MODEL_MARKERS`)."""
    lowered = model_id.lower()
    return any(marker in lowered for marker in _CHINESE_MODEL_MARKERS)


def assert_chinese_model_allowed(model_id: str, *, chinese_models_allowed: bool | None = None) -> None:
    """Raise ``ValueError`` when *model_id* is Chinese-origin and disallowed for the active overlay.

    *chinese_models_allowed* is injectable for tests; the default reads the
    resolved ``chinese_models_allowed`` DB-home setting (#2887) — ``True`` for
    teatree's own default posture, overridden ``False`` per-overlay by an
    overlay serving client work under a no-Chinese-models policy. A non-Chinese
    *model_id*, or an allowed one, is a no-op.
    """
    allowed = (
        chinese_models_allowed
        if chinese_models_allowed is not None
        else get_effective_settings().chinese_models_allowed
    )
    if not allowed and is_chinese_origin_model(model_id):
        msg = (
            f"model {model_id!r} is a Chinese-origin model but chinese_models_allowed is False "
            "for the active overlay; set a non-Chinese tier/model or "
            "`t3 <overlay> config_setting set chinese_models_allowed true --overlay <name>`"
        )
        raise ValueError(msg)


def resolve_tier(tier: str, *, config_path: Path | None = None) -> str:
    """Resolve an abstract *tier* name to its concrete model id.

    Reads :data:`TIER_MODELS`, with each entry OVERRIDABLE via the
    ``[agent.tier_models]`` config table (merged OVER the shipped default), so a
    new model is adopted in one place — this constant or one config line. An
    unknown *tier* (not a :data:`TIER_MODELS` key, not overridden) is passed
    through unchanged: the caller may legitimately pass a concrete model id where
    a tier is expected, and a genuine typo surfaces downstream rather than being
    silently swallowed here.
    """
    config = resolve_agent_config(config_path=config_path)
    merged = {**TIER_MODELS, **config.tier_models}
    return merged.get(tier, tier)


def _resolve_pydantic_ai_tier(tier: str, *, config_path: Path | None = None) -> str:
    """Resolve an abstract *tier* to its OrcaRouter router handle for the pydantic_ai harness.

    The :func:`resolve_tier` sibling for the ``pydantic_ai`` harness: reads
    :data:`PYDANTIC_AI_TIER_MODELS`, each entry OVERRIDABLE via
    ``[agent.pydantic_ai_tier_models]`` (merged OVER the shipped default). Unlike
    :func:`resolve_tier` — which passes an unknown *tier* through unchanged so a
    caller may hand it a concrete id — an unknown tier here falls back to the
    :data:`DEFAULT_TIER` handle: the ``pydantic_ai`` harness MUST resolve to a
    handle OrcaRouter's catalog carries, never a bare tier name it would reject.
    """
    config = resolve_agent_config(config_path=config_path)
    merged = {**PYDANTIC_AI_TIER_MODELS, **config.pydantic_ai_tier_models}
    return merged.get(tier) or merged.get(DEFAULT_TIER, PYDANTIC_AI_TIER_MODELS[DEFAULT_TIER])


def resolve_pydantic_ai_model(model_name: str | None, *, config_path: Path | None = None) -> str:
    """Normalise a resolved model id for the ``pydantic_ai`` (OrcaRouter) harness.

    THE dash-form id normalisation (OrcaRouter setup plan §3.2). teatree's abstract
    tiers resolve (via :data:`TIER_MODELS`) to Claude ids in DASH-form
    (``claude-opus-4-8``) that OrcaRouter's catalog does NOT carry — Orca serves
    PROVIDER-PREFIXED ids (``anthropic/claude-opus-4.8``, ``deepseek/deepseek-v4-pro``,
    ``orcarouter/teatree-factory``). So a teatree-native id (no provider ``/``
    prefix — the :data:`TIER_MODELS` default form, or ``None``) is normalised UP to
    the OrcaRouter router handle for the id's abstract tier
    (:func:`_resolve_pydantic_ai_tier`). An explicit Orca-native pin (ANY
    provider-prefixed id, e.g. an operator ``phase_models`` override to
    ``deepseek/deepseek-v4-pro``) passes through UNCHANGED — the caller then still
    runs it past :func:`assert_chinese_model_allowed`.
    """
    if model_name and "/" in model_name:
        return model_name
    return _resolve_pydantic_ai_tier(_abstract_tier_of(model_name), config_path=config_path)


def _abstract_tier_of(model_name: str | None) -> str:
    """The abstract tier a teatree-native Claude id belongs to, else :data:`DEFAULT_TIER`."""
    lowered = (model_name or "").lower()
    for family, tier in _CLAUDE_FAMILY_TO_TIER.items():
        if family in lowered:
            return tier
    return DEFAULT_TIER


def resolve_tier_effort(
    tier: str, *, harness: AgentHarness | None = None, config_path: Path | None = None
) -> str | None:
    """Resolve an abstract *tier* name to its reasoning EFFORT — the effort parallel of :func:`resolve_tier`.

    Reads :data:`TIER_EFFORT`, with each entry OVERRIDABLE via the
    ``[agent.tier_effort]`` config table (merged OVER the shipped default). Unlike
    :func:`resolve_tier` — which passes an unknown *tier* through unchanged so a
    caller may hand it a concrete model id — an unknown tier here returns ``None``:
    a tier with no effort entry (the ``cheap``/Haiku tier, or a ``phase_models``
    override that named a concrete model id) means "pin no effort, inherit the SDK
    default", never "pass a bogus value to ``--effort``".

    HARNESS-SCOPED ([#2885](https://github.com/souliane/teatree/issues/2885)): the
    resolved value is validated against :data:`HARNESS_EFFORT_SCALE` for *harness*
    (default: the resolved ``agent_harness`` DB-home setting) and dropped — falling
    back to the merged default — when it is outside that harness's vocabulary. The
    shipped defaults (``xhigh`` / ``high``) are valid on every harness's scale today,
    so this only narrows an off-harness ``[agent.tier_effort]`` override (e.g. a
    ``claude_sdk``-only ``"max"`` reaching a ``pydantic_ai`` spawn).
    """
    harness = harness if harness is not None else get_effective_settings().agent_harness
    allowed = HARNESS_EFFORT_SCALE[harness]
    config = resolve_agent_config(config_path=config_path)
    merged = {**TIER_EFFORT, **config.tier_effort}
    resolved = merged.get(tier)
    if resolved is not None and resolved not in allowed:
        # The override is off the active harness's scale — fall back to the
        # shipped default; if even THAT is somehow off-scale (not the case for
        # any shipped tier today), drop to None rather than emit a bogus value.
        resolved = TIER_EFFORT.get(tier)
        if resolved is not None and resolved not in allowed:
            resolved = None
    return resolved


def resolve_phase_model(phase: str, *, config_path: Path | None = None) -> str | None:
    """Resolve the concrete Claude model id for *phase* — phase → tier → model.

    Resolution order, first match wins:

    1.  A config override in ``[agent] phase_models.<phase>`` of
        ``~/.teatree.toml``. A sentinel value (empty / ``"default"`` /
        ``"inherit"``) disables tiering for that phase (returns ``None``); any
        other override value is resolved through :func:`resolve_tier` — so it may
        name a TIER (``"frontier"``) or a concrete model id (passed through).
    2.  The phase's tier in :data:`DEFAULT_PHASE_MODELS`, resolved through
        :func:`resolve_tier`.
    3.  A phase NOT in :data:`DEFAULT_PHASE_MODELS` falls back to
        :data:`DEFAULT_TIER`, resolved through :func:`resolve_tier`.

    ``None`` is returned ONLY for a sentinel override — meaning the caller must
    not pass ``--model`` and the user's default model applies.
    """
    overrides = _load_phase_model_overrides(config_path)
    if phase in overrides:
        value = overrides[phase].strip()
        if value.lower() in _INHERIT_SENTINELS:
            return None
        return resolve_tier(value, config_path=config_path)
    tier = DEFAULT_PHASE_MODELS.get(phase, DEFAULT_TIER)
    return resolve_tier(tier, config_path=config_path)


def resolve_spawn_model(
    phase: str,
    *,
    skills: Iterable[str],
    session_id: str | None = None,
    task_id: int | None = None,
    config_path: Path | None = None,
) -> str | None:
    """Resolve the spawn model: the phase model raised by the per-skill floors.

    Starts from :func:`resolve_phase_model` (the per-phase tier resolved to a
    concrete model id) and merges in the ``[agent.skill_models]`` MODEL floor of
    every loaded skill in *skills*. The merge is *most-capable-wins*: a floor can
    only RAISE the resulting model's capability (via
    :func:`teatree.core.cost.tier_rank`, which ranks an abstract tier, an old
    short-name, and a concrete dated id identically), never lower it, so the
    merge is order-independent. A skill with no floor entry, or one whose floor
    is an inherit sentinel (``None`` after normalisation), contributes nothing.

    After the floor merge, a SITUATIONAL honesty-critical escalation
    (teatree#2263) can RAISE the winner to ``[agent] honesty_model`` (default
    ``"opus"``): when *phase* is a :data:`VERIFICATION_PHASES` phase AND an
    active :class:`~teatree.core.models.honesty_escalation.HonestyEscalation`
    row exists for *session_id*. It is most-capable-wins (only raises, never
    lowers) and gated, so with no active escalation (or both ids ``None``) it is
    a no-op and resolution is byte-identical to today.

    Returns ``None`` only when the phase model resolved to ``None`` (a sentinel
    ``phase_models`` override that opts the phase out of tiering) AND no skill
    floor applies — the caller then passes no ``--model`` and the user's default
    model applies. Every other phase resolves to a concrete model id (phase →
    tier → model). MODEL only: there is no per-skill effort axis — the reasoning
    effort is per-abstract-TIER, resolved separately for the same spawn by
    :func:`resolve_spawn_effort`.

    The honesty-escalation raise (below) is the LAST step of resolution — nothing
    downgrades the winner afterward, so the escalated model is exactly what the
    spawn receives.
    """
    config = resolve_agent_config(config_path=config_path)
    winner = resolve_phase_model(phase, config_path=config_path)
    for skill in skills:
        floor = config.skill_models.get(skill)
        if floor is not None and tier_rank(floor) > tier_rank(winner):
            winner = resolve_tier(floor, config_path=config_path)
    if (
        _is_verification_phase(phase)
        and _honesty_escalation_active(session_id, task_id)
        and tier_rank(config.honesty_model) > tier_rank(winner)
    ):
        winner = resolve_tier(config.honesty_model, config_path=config_path)
    return winner


def resolve_spawn_effort(
    phase: str, *, harness: AgentHarness | None = None, config_path: Path | None = None
) -> str | None:
    """Resolve the spawn EFFORT for *phase* — phase → tier → effort, the effort parallel of :func:`resolve_spawn_model`.

    Mirrors :func:`resolve_phase_model`'s resolution, swapping the model constant
    for the effort constant: the same ``[agent] phase_models`` override mechanism
    picks the abstract tier, then :func:`resolve_tier_effort` maps that tier to its
    reasoning effort. So a ``phase_models`` override to a cheaper tier lowers BOTH
    the model and the effort in lock-step, and a sentinel override (empty /
    ``default`` / ``inherit``) returns ``None`` (pin no ``--effort``).

    ``None`` is returned whenever the resolved tier has no effort entry — the
    ``cheap``/Haiku phases, a phase pinned to a concrete model id (not a tier), or
    a sentinel override — so those spawns inherit the SDK default effort. There is
    no per-skill effort axis: unlike :func:`resolve_spawn_model`, skill floors and
    the honesty escalation raise only the MODEL, never the phase's effort tier.

    *harness* threads straight through to :func:`resolve_tier_effort` (default:
    the resolved ``agent_harness`` setting), so the caller building
    ``ClaudeAgentOptions`` for the CURRENTLY ACTIVE harness — whichever backend
    :func:`teatree.agents.harness.resolve_harness` will hand those options to —
    always gets a value that harness understands.
    """
    overrides = _load_phase_model_overrides(config_path)
    if phase in overrides:
        value = overrides[phase].strip()
        if value.lower() in _INHERIT_SENTINELS:
            return None
        return resolve_tier_effort(value, harness=harness, config_path=config_path)
    tier = DEFAULT_PHASE_MODELS.get(phase, DEFAULT_TIER)
    return resolve_tier_effort(tier, harness=harness, config_path=config_path)


def model_supports_thinking(model: str | None) -> bool:
    """Whether *model* accepts an explicit adaptive-thinking pin — fail-SAFE.

    Production spawns set ``thinking={"type": "adaptive"}`` EXPLICITLY so the
    Opus-4.8 reasoning phases deterministically think — Opus 4.8 runs WITHOUT
    thinking when the option is omitted (unlike Sonnet 5, which defaults to
    adaptive). The cheap/Haiku tier rejects the ``thinking`` / ``effort`` levers,
    so this GUARD returns ``False`` for a Haiku model (matched on the tier via
    :func:`teatree.core.cost.tier_of_model`, so a future dated Haiku id is
    covered). ``None`` (the inherit sentinel — the caller adds no ``--model`` and
    the user's own default applies) also returns ``False``: the inherited model
    is unknown here, so the safe choice is to leave the SDK default rather than
    force a pin the default might reject.
    """
    if not model:
        return False
    return tier_of_model(model) != _NON_THINKING_TIER


def _is_verification_phase(phase: str) -> bool:
    """Whether *phase* is one the honesty escalation routes (a verification phase)."""
    return phase in VERIFICATION_PHASES


def _honesty_escalation_active(session_id: str | None, task_id: int | None) -> bool:
    """Whether an active honesty escalation exists for *session_id*/*task_id* — fail-SAFE.

    A thin wrapper over
    :meth:`teatree.core.models.honesty_escalation.HonestyEscalation.is_active`,
    wrapped ``try/except → False`` (same fail-to-no-effect posture as
    :func:`_load_phase_model_overrides`). A blank *session_id* or ANY resolution
    error (an import problem, a DB error) returns ``False`` so the escalation
    silently no-ops — a resolution error must NEVER silently escalate.
    """
    if not session_id:
        return False
    try:
        from teatree.core.models import HonestyEscalation  # noqa: PLC0415

        return HonestyEscalation.is_active(session_id, task_id=task_id)
    except Exception:  # noqa: BLE001
        return False


def _load_phase_model_overrides(config_path: Path | None) -> dict[str, str]:
    """Read the ``[agent] phase_models`` table from the toml config.

    Returns an empty mapping when the file or section is absent or malformed
    so the shipped defaults always apply.
    """
    path = config_path if config_path is not None else CONFIG_PATH
    if not path.is_file():
        return {}
    try:
        with path.open("rb") as fh:
            raw = tomllib.load(fh)
    except (tomllib.TOMLDecodeError, OSError):
        return {}
    agent_section = raw.get("agent", {})
    phase_models = agent_section.get("phase_models", {})
    if not isinstance(phase_models, dict):
        return {}
    return {str(phase): str(model) for phase, model in phase_models.items()}
