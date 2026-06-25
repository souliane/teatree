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

The mapping is config-driven via ``~/.teatree.toml``::

    [agent]
    phase_models.reviewing = "frontier"  # pin a phase to a tier (or a model id)
    phase_models.coding = "balanced"     # opt a phase into a cheaper tier
    phase_models.testing = ""            # opt out — inherit the user's default

    [agent.tier_models]
    frontier = "claude-opus-4-9"         # adopt a new frontier model, one line

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
from teatree.config_agent import _INHERIT_SENTINELS, AgentConfig, resolve_agent_config
from teatree.core.cost import tier_of_model, tier_rank

# THE SINGLE SOURCE OF TRUTH for concrete model ids. This is the ONLY place a
# concrete Claude model id appears in teatree's model-resolution code: abstract
# tier name -> concrete model id. Overridable per tier via ``[agent.tier_models]``
# (merged OVER this default), so adopting a new model is one edit here or one
# config line — no scenario, test, or dispatch edit.
TIER_MODELS: dict[str, str] = {
    "frontier": "claude-opus-4-8",
    "balanced": "claude-sonnet-4-6",
    "cheap": "claude-haiku-4-5",
}

# The default tier for a phase NOT in :data:`DEFAULT_PHASE_MODELS`, and the
# default tier for an eval scenario that declares neither ``model:`` nor ``tier:``
# nor ``phase:``. The conservative mid tier.
DEFAULT_TIER = "balanced"

# The :func:`teatree.core.cost.tier_of_model` tier key for Fable. Normalising a
# model id to its tier recognises BOTH the short alias ``fable`` and the full
# ``claude-fable-5`` (and any future dated Fable id), so the kill-switch matches
# on the tier rather than a brittle ``== "fable"`` string compare. Fable is the
# most-honest escalation tier — deliberately NOT a member of :data:`TIER_MODELS`
# (it is access-gated/disabled), so it never appears as a routine phase tier.
_FABLE_TIER = "fable"

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
    (teatree#2263) can RAISE the winner to ``[agent] honesty_model`` (today
    Fable): when *phase* is a :data:`VERIFICATION_PHASES` phase AND an active
    :class:`~teatree.core.models.honesty_escalation.HonestyEscalation` row
    exists for *session_id*. It is most-capable-wins (only raises, never lowers)
    and gated, so with no active escalation (or both ids ``None``) it is a no-op
    and resolution is byte-identical to today.

    Returns ``None`` only when the phase model resolved to ``None`` (a sentinel
    ``phase_models`` override that opts the phase out of tiering) AND no skill
    floor applies — the caller then passes no ``--model`` and the user's default
    model applies. Every other phase resolves to a concrete model id (phase →
    tier → model). MODEL only: there is no per-skill effort axis (effort is a
    session-wide pin set on the interactive loop spawn).

    The resolved winner passes through :func:`_downgrade_fable` last: with the
    ``[agent] fable_enabled`` kill-switch off (teatree#2237), a Fable winner
    transparently downgrades to ``fable_fallback`` (Opus 4.8 baseline). This is
    the single spawn chokepoint, so the downgrade covers every sub-agent spawn
    — including an honesty-escalated Fable, which must still pass the kill-switch
    (LOAD-BEARING ordering: the escalation raise lands BEFORE this call).
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
    return _downgrade_fable(winner, config)


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
    silently no-ops — a resolution error must NEVER silently pin Fable.
    """
    if not session_id:
        return False
    try:
        from teatree.core.models import HonestyEscalation  # noqa: PLC0415

        return HonestyEscalation.is_active(session_id, task_id=task_id)
    except Exception:  # noqa: BLE001
        return False


def _downgrade_fable(model: str | None, config: AgentConfig) -> str | None:
    """Apply the single Fable kill-switch to one resolved *model* (teatree#2237).

    When *model* is Fable — recognised by tier (the short alias ``fable`` OR the
    full ``claude-fable-5``, via :func:`teatree.core.cost.tier_of_model`) — AND
    ``config.fable_enabled`` is ``False``, return ``config.fable_fallback`` (the
    Opus 4.8 baseline by default). Otherwise return *model* unchanged: a
    non-Fable model, ``None`` (inherit), or Fable while the switch is on all pass
    through untouched, so enabled/absent is byte-identical to today.
    """
    if model is None or config.fable_enabled:
        return model
    if tier_of_model(model) == _FABLE_TIER:
        return config.fable_fallback
    return model


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
