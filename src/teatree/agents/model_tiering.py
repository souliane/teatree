"""Per-phase headless model tiering by ABSTRACT TIER (#880, #562 §3).

Every concrete model id in teatree lives in EXACTLY ONE place: the
:data:`TIER_MODELS` constant below. Everything else — production phase dispatch,
eval scenarios, the benchmark, and the tests — references an abstract TIER
(``frontier`` / ``balanced`` / ``cheap``), never a concrete model id. Adopting a
new model is one edit to :data:`TIER_MODELS` (or one ``[agent.tier_models]`` TOML
line), with zero scenario, test, or dispatch edits.

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

from teatree.config import CONFIG_PATH
from teatree.config_agent import _INHERIT_SENTINELS, resolve_agent_config
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


def resolve_tier_effort(tier: str, *, config_path: Path | None = None) -> str | None:
    """Resolve an abstract *tier* name to its reasoning EFFORT — the effort parallel of :func:`resolve_tier`.

    Reads :data:`TIER_EFFORT`, with each entry OVERRIDABLE via the
    ``[agent.tier_effort]`` config table (merged OVER the shipped default). Unlike
    :func:`resolve_tier` — which passes an unknown *tier* through unchanged so a
    caller may hand it a concrete model id — an unknown tier here returns ``None``:
    a tier with no effort entry (the ``cheap``/Haiku tier, or a ``phase_models``
    override that named a concrete model id) means "pin no effort, inherit the SDK
    default", never "pass a bogus value to ``--effort``".
    """
    config = resolve_agent_config(config_path=config_path)
    merged = {**TIER_EFFORT, **config.tier_effort}
    return merged.get(tier)


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


def resolve_spawn_effort(phase: str, *, config_path: Path | None = None) -> str | None:
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
    """
    overrides = _load_phase_model_overrides(config_path)
    if phase in overrides:
        value = overrides[phase].strip()
        if value.lower() in _INHERIT_SENTINELS:
            return None
        return resolve_tier_effort(value, config_path=config_path)
    tier = DEFAULT_PHASE_MODELS.get(phase, DEFAULT_TIER)
    return resolve_tier_effort(tier, config_path=config_path)


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
