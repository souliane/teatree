"""Resolution of the ``[agent]`` config table's spawn-model + session pins (teatree#2216).

Mirrors the :mod:`teatree.config_speak` / :class:`~teatree.types.SpeakConfig`
precedent — a frozen dataclass plus a typed sub-table parser — but reads raw
``tomllib`` directly like the sibling ``phase_models`` loader in
:mod:`teatree.agents.model_tiering`, because these values are session-scoped
spawn inputs read by the dispatch paths, not part of the per-overlay
``get_effective_settings`` merge.

Three settings, all under ``[agent]`` in ``~/.teatree.toml`` (composing with
the existing ``[agent.phase_models]`` table)::

    [agent]
    session_model = "fable"       # interactive main-agent --model pin
    session_effort = "xhigh"      # interactive main-agent --effort pin

    [agent.skill_models]          # per-companion-skill MODEL floor (no effort axis)
    code-review = "opus"
    architecture-design = "fable"

    [agent.tier_models]           # override the concrete model id of an abstract tier
    frontier = "claude-opus-4-9"  # adopt a new frontier model with one config line

The per-skill floor is MODEL only — effort is settable session-wide (on the
interactive loop spawn) and never per-sub-agent, so there is deliberately no
``skill_effort`` axis.

The ``[agent.tier_models]`` table mirrors ``[agent.skill_models]``: each entry
overrides the concrete model id a tier resolves to, merged OVER the
:data:`teatree.agents.model_tiering.TIER_MODELS` shipped default. It is the
config escape hatch for the "single source of truth" model constant — adopting a
new model is one TOML line, with no code edit.

:data:`_INHERIT_SENTINELS` lives here (foundation) rather than in
:mod:`teatree.agents.model_tiering` (domain) so a model value can be normalised
to ``None`` at this boundary without ``config_agent`` importing UP into
``agents``; ``model_tiering`` re-imports the set so the two layers share one
definition of "this value means inherit the default".
"""

import tomllib
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path

from teatree.config import CONFIG_PATH

# Model values that explicitly opt out of a floor / pin — normalised to ``None``
# (inherit the default model, emit no ``--model`` flag). Shared with
# :mod:`teatree.agents.model_tiering`, which re-imports this set.
_INHERIT_SENTINELS = frozenset({"", "default", "inherit"})

# The strict CLI effort scale, weakest to strongest. ``max`` is the ceiling
# (above ``xhigh``); there is no ``off`` — effort is always one of these five.
EFFORT_SCALE = frozenset({"low", "medium", "high", "xhigh", "max"})


def parse_effort(value: object) -> str | None:
    """Validate a ``session_effort`` value against :data:`EFFORT_SCALE`.

    ``None`` (absent) returns ``None``. Any present value must be a member of
    the strict CLI scale ``low | medium | high | xhigh | max`` (case- and
    whitespace-insensitive); anything else — including ``"off"`` and the empty
    string — raises :class:`ValueError`, mirroring
    :meth:`teatree.types.LocalPlayback.parse`.
    """
    if value is None:
        return None
    normalised = value.strip().lower() if isinstance(value, str) else value
    if normalised in EFFORT_SCALE:
        return str(normalised)
    valid = ", ".join(sorted(EFFORT_SCALE))
    message = f"Invalid session effort {value!r}; valid values: {valid}"
    raise ValueError(message)


def _normalize_model(value: object) -> str | None:
    """Normalise a model value: strip + map the inherit sentinels to ``None``."""
    text = str(value).strip()
    if text.lower() in _INHERIT_SENTINELS:
        return None
    return text


@dataclass(frozen=True)
class AgentConfig:
    """The resolved ``[agent]`` spawn-model + session-pin settings (teatree#2216).

    *   ``skill_models`` — companion-skill-name → model floor (``None`` for a
        skill explicitly opted out via an inherit sentinel). MODEL only.
    *   ``tier_models`` — abstract-tier-name → concrete model id, merged OVER
        :data:`teatree.agents.model_tiering.TIER_MODELS`. The config escape hatch
        for the single model constant: adopting a new model for a tier is one TOML
        line. Empty by default → the shipped :data:`TIER_MODELS` stands unchanged.
        Mirrors ``skill_models`` (a typed ``[agent.tier_models]`` sub-table); a
        non-table value or a non-string entry value yields no override.
    *   ``session_model`` — the interactive main-agent ``--model`` pin, or
        ``None`` to inherit the user's default.
    *   ``session_effort`` — the interactive main-agent ``--effort`` pin (a
        member of :data:`EFFORT_SCALE`), or ``None`` to inherit.
    *   ``fable_enabled`` — the single Fable kill-switch (teatree#2237).
        ``True`` (the default, and absent-key) keeps every Fable pin resolving
        to Fable, byte-identical to today. ``False`` transparently downgrades
        every resolved Fable model value to :attr:`fable_fallback` across all
        spawn surfaces and the session pin, so reverting to the Opus 4.8
        baseline is one flip rather than editing every Fable pin.
    *   ``fable_fallback`` — the model Fable downgrades to when disabled.
        Default ``"opus"`` (which the tier/cost machinery maps to
        ``claude-opus-4-8``), so Opus 4.8 compatibility is preserved by
        construction. Normalised through :func:`_normalize_model`.
    *   ``honesty_model`` — the most-honest model a situational honesty-critical
        escalation routes verification spawns to (teatree#2263). Default
        ``"fable"`` so "today Fable, tomorrow the most-honest model" is a one-line
        config edit. Normalised through :func:`_normalize_model`; an absent key
        or a sentinel value falls back to ``"fable"`` (a concrete model id, never
        the inherit sentinel — the escalation must route to a real model). The
        escalation still passes through :func:`teatree.agents.model_tiering._downgrade_fable`,
        so the ``fable_enabled`` kill-switch reverts an escalated Fable too.
    *   ``phase_fanout`` — per-``(role, phase)`` fan-out opt-in (teatree#2229),
        keyed canonical ``"role:phase"`` (e.g. ``"reviewer:reviewing"``). A
        ``bool`` value enables the registry default ``fanout_n`` (``True``) or
        disables (``False``); an ``int`` value enables and overrides the panel
        width. Empty by default → ``core.phases.resolve_fanout_directive``
        renders nothing → dispatch is byte-identical to today until a pair is
        opted in. Mirrors the ``skill_models`` precedent (a typed
        ``[agent.phase_fanout]`` sub-table); the int range is validated at
        directive-render time (``core.phases._resolved_fanout_n``), fail-loud.
    """

    skill_models: dict[str, str | None] = field(default_factory=dict)
    session_model: str | None = None
    session_effort: str | None = None
    fable_enabled: bool = True
    fable_fallback: str = "opus"
    phase_fanout: dict[str, bool | int] = field(default_factory=dict)
    honesty_model: str = "fable"
    tier_models: dict[str, str] = field(default_factory=dict)


def _phase_fanout_from(raw: object) -> dict[str, bool | int]:
    """Normalise the ``[agent.phase_fanout]`` table into a ``"role:phase" → opt-in`` map.

    Each value is a ``bool`` (enable at registry default / disable) or an
    ``int`` (enable + override the panel width). ``bool`` is checked before
    ``int`` because ``bool`` is an ``int`` subclass — a bare ``true``/``false``
    must stay a bool, not collapse to ``1``/``0``. A non-table value (a
    malformed scalar) yields an empty map, and any non-bool/non-int entry value
    is skipped, matching the ``skill_models`` loader's tolerance. The int range
    is NOT validated here — it is validated fail-loud at render time
    (``core.phases._resolved_fanout_n``) so a bad N surfaces with the rendering
    context, not as a silent drop at parse time.
    """
    if not isinstance(raw, dict):
        return {}
    resolved: dict[str, bool | int] = {}
    for pair, opt_in in raw.items():
        # ``bool | int`` accepts both (``bool`` is an ``int`` subclass); a
        # ``bool`` value stays a ``bool`` because it is the runtime type, never
        # coerced to ``1``/``0``. Non-bool/non-int values are skipped.
        if isinstance(opt_in, bool | int):
            resolved[str(pair)] = opt_in
    return resolved


def _skill_models_from(raw: object) -> dict[str, str | None]:
    """Normalise the ``[agent.skill_models]`` table into a floor map.

    Each value is normalised through the inherit sentinels (sentinel → ``None``).
    A non-table value (a malformed scalar) yields an empty floor map, matching
    the ``phase_models`` loader's tolerance.
    """
    if not isinstance(raw, dict):
        return {}
    return {str(skill): _normalize_model(model) for skill, model in raw.items()}


def _tier_models_from(raw: object) -> dict[str, str]:
    """Normalise the ``[agent.tier_models]`` table into a tier → model-id override map.

    Each entry overrides the concrete model id a tier resolves to. A non-table
    value yields an empty map, and a non-string-or-blank entry value is skipped —
    the same tolerance as ``skill_models`` — so a malformed override never
    poisons the shipped :data:`teatree.agents.model_tiering.TIER_MODELS` default.
    """
    if not isinstance(raw, dict):
        return {}
    resolved: dict[str, str] = {}
    for tier, model in raw.items():
        if isinstance(model, str) and model.strip():
            resolved[str(tier)] = model.strip()
    return resolved


def _fable_fallback_from(raw: object) -> str:
    """Normalise the ``[agent] fable_fallback`` value to a non-empty model id.

    Shares :func:`_normalize_model`'s boundary (whitespace strip + sentinel
    handling). An absent key or a sentinel value (which normalises to ``None``)
    falls back to the Opus 4.8 baseline ``"opus"`` — the fallback must always be
    a concrete model id, never the inherit sentinel.
    """
    if raw is None:
        return "opus"
    return _normalize_model(raw) or "opus"


def _honesty_model_from(raw: object) -> str:
    """Normalise the ``[agent] honesty_model`` value to a non-empty model id.

    Shares :func:`_normalize_model`'s boundary (whitespace strip + sentinel
    handling). An absent key or a sentinel value (which normalises to ``None``)
    falls back to ``"fable"`` — the escalation target must always be a concrete
    model id, never the inherit sentinel.
    """
    if raw is None:
        return "fable"
    return _normalize_model(raw) or "fable"


def _agent_config_from_table(agent: Mapping[str, object]) -> AgentConfig:
    """Build an :class:`AgentConfig` from the parsed ``[agent]`` table.

    Effort is validated here (raises on an off-scale value); model values are
    normalised through the inherit sentinels. The Fable kill-switch defaults to
    enabled (absent key == enabled) so existing Fable-pinned users are unchanged.
    """
    return AgentConfig(
        skill_models=_skill_models_from(agent.get("skill_models")),
        session_model=_normalize_model(agent["session_model"]) if "session_model" in agent else None,
        session_effort=parse_effort(agent.get("session_effort")),
        fable_enabled=bool(agent.get("fable_enabled", True)),
        fable_fallback=_fable_fallback_from(agent.get("fable_fallback")),
        phase_fanout=_phase_fanout_from(agent.get("phase_fanout")),
        honesty_model=_honesty_model_from(agent.get("honesty_model")),
        tier_models=_tier_models_from(agent.get("tier_models")),
    )


def resolve_agent_config(*, config_path: Path | None = None) -> AgentConfig:
    """Resolve the effective :class:`AgentConfig` from ``~/.teatree.toml``.

    A missing file, a missing ``[agent]`` section, or malformed TOML all yield
    the default :class:`AgentConfig` (empty floor map, no pins) — the same
    fail-to-defaults posture as the ``phase_models`` loader — so installing
    this consumer changes no behaviour until the user configures a value. An
    explicitly-set *invalid* ``session_effort`` still raises (fail loud), since
    that is a misconfiguration the user must see, not an absence to tolerate.
    """
    path = config_path if config_path is not None else CONFIG_PATH
    if not path.is_file():
        return AgentConfig()
    try:
        with path.open("rb") as fh:
            raw = tomllib.load(fh)
    except (tomllib.TOMLDecodeError, OSError):
        return AgentConfig()
    agent = raw.get("agent", {})
    if not isinstance(agent, Mapping):
        return AgentConfig()
    return _agent_config_from_table(agent)
