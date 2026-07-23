"""Resolution of the ``[agent]`` spawn-model + session pins (teatree#2216).

Each ``[agent]`` setting is read from the DB ``ConfigSetting`` store via
:mod:`teatree.config.cold_reader` (a Django-free reader of the canonical DB),
one DB key per setting. Set a value with
``t3 <overlay> config_setting set <key> <value>``::

    t3 <overlay> config_setting set agent_session_model opus
    t3 <overlay> config_setting set agent_session_effort xhigh
    t3 <overlay> config_setting set agent_skill_models '{"code-review": "opus"}'
    t3 <overlay> config_setting set agent_tier_models '{"frontier": "claude-opus-4-9"}'
    t3 <overlay> config_setting set agent_pydantic_ai_tier_models '{"frontier": "vendor/some-model"}'
    t3 <overlay> config_setting set agent_tier_effort '{"balanced": "xhigh"}'

The per-skill floor (``agent_skill_models``) is MODEL only — there is
deliberately no ``skill_effort`` axis. The reasoning-effort dial is
per-ABSTRACT-TIER instead, via ``agent_tier_effort`` (which reaches every
sub-agent spawn through
:func:`teatree.agents.model_tiering.resolve_spawn_effort`), while
``agent_session_effort`` remains the separate interactive main-agent pin.

``agent_tier_models`` mirrors ``agent_skill_models``: each entry overrides the
concrete model id a tier resolves to, merged OVER the
:data:`teatree.agents.model_tiering.TIER_MODELS` shipped default. It is the
config escape hatch for the "single source of truth" model constant — adopting a
new model is one DB row, with no code edit.

``agent_tier_effort`` mirrors ``agent_tier_models`` exactly: each entry
overrides the reasoning effort an abstract tier spawns with, merged OVER the
:data:`teatree.agents.model_tiering.TIER_EFFORT` shipped default. Each value
must be a member of :data:`EFFORT_SCALE` (an off-scale value is dropped, matching
the ``agent_tier_models`` tolerance), so a malformed override never poisons the
shipped per-tier effort.

:data:`_INHERIT_SENTINELS` lives here (foundation) rather than in
:mod:`teatree.agents.model_tiering` (domain) so a model value can be normalised
to ``None`` at this boundary without ``config.agent_spawn`` importing UP into
``agents``; ``model_tiering`` re-imports the set so the two layers share one
definition of "this value means inherit the default".
"""

import os
from dataclasses import dataclass, field

from teatree.config import cold_reader
from teatree.config.agent_enums import AgentHarness

# Model values that explicitly opt out of a floor / pin — normalised to ``None``
# (inherit the default model, emit no ``--model`` flag). Shared with
# :mod:`teatree.agents.model_tiering`, which re-imports this set.
_INHERIT_SENTINELS = frozenset({"", "default", "inherit"})

# The strict CLI effort scale, weakest to strongest. ``max`` is the ceiling
# (above ``xhigh``); there is no ``off`` — effort is always one of these five.
EFFORT_SCALE = frozenset({"low", "medium", "high", "xhigh", "max"})

# The shipped default for the interactive main-agent effort pin when
# ``agent_session_effort`` is unset — the interactive session runs at ``xhigh``
# unless the operator pins a different scale value.
DEFAULT_SESSION_EFFORT = "xhigh"


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
        for the single model constant: adopting a new model for a tier is one DB
        row. Empty by default → the shipped :data:`TIER_MODELS` stands unchanged.
        Mirrors ``skill_models`` (a typed ``agent_tier_models`` dict); a
        non-dict value or a non-string entry value yields no override.
    *   ``pydantic_ai_tier_models`` — the ``tier_models`` sibling for the
        ``pydantic_ai`` (OpenAI-compatible) harness: abstract-tier-name → provider-native
        model/router-handle id, merged OVER
        :data:`teatree.agents.model_tiering.PYDANTIC_AI_TIER_MODELS`. Distinct from
        ``tier_models`` because the two harnesses target different catalogs — the
        ``claude_sdk`` harness serves Claude dash-form ids, the ``pydantic_ai``
        harness serves the provider's own prefixed ids. Empty by default → the
        shipped router-handle default stands. Same mechanics/tolerance as
        ``tier_models``.
    *   ``tier_effort`` — abstract-tier-name → reasoning effort, merged OVER
        :data:`teatree.agents.model_tiering.TIER_EFFORT`. The per-tier reasoning
        dial that reaches every sub-agent spawn. Empty by default → the shipped
        :data:`TIER_EFFORT` stands unchanged. Mirrors ``tier_models`` (a typed
        ``agent_tier_effort`` dict); a non-dict value, a non-string entry,
        or a value off :data:`EFFORT_SCALE` yields no override.
    *   ``session_model`` — the interactive main-agent ``--model`` pin, or
        ``None`` to inherit the user's default.
    *   ``session_effort`` — the interactive main-agent ``--effort`` pin (a
        member of :data:`EFFORT_SCALE`). Defaults to
        :data:`DEFAULT_SESSION_EFFORT` (``xhigh``) when unset.
    *   ``session_permission_mode`` — the ``t3 loop start`` ``--permission-mode``
        override (#3528). Empty (the default) means NO opinion, and the CLI keeps
        its shipped ``permission_modes.UNATTENDED`` pin — which is what makes
        ``t3 doctor check``'s ``auto`` advice safe to follow. The setting exists so
        an operator can narrow it without editing teatree. The default lives at the
        CLI rather than here because this platform layer may not import
        ``teatree.agents``. ``T3_AGENT_SESSION_PERMISSION_MODE`` wins over the
        stored value.
    *   ``honesty_model`` — the most-honest model a situational honesty-critical
        escalation routes verification spawns to (teatree#2263). Default
        ``"opus"`` — the frontier-tier baseline, requiring no operator opt-in.
        A stronger or different escalation target is a one-line config edit
        (``agent_honesty_model``). Normalised through :func:`_normalize_model`;
        an absent key or a sentinel value falls back to ``"opus"`` (a concrete
        model id, never the inherit sentinel — the escalation must route to a
        real model).
    *   ``phase_harness`` — per-FSM-phase harness override, keyed by canonical
        phase (e.g. ``"reviewing"``). Each value is an :class:`AgentHarness`
        (flip the pin) or ``None`` (an explicit unpin — the phase drops back to
        the configured ``agent_harness``). Merged OVER
        :data:`teatree.agents.model_tiering.PHASE_HARNESS` (the verification-phase
        ``claude_sdk`` pin) by :func:`~teatree.agents.model_tiering.resolve_phase_harness`,
        so an EMPTY map is byte-identical to the shipped pin — a full swap of the
        verification checker onto ``pydantic_ai`` becomes one ``agent_phase_harness``
        DB row instead of a code edit (§3a #3). Mirrors ``tier_models`` tolerance:
        a non-dict value, a non-string entry, or a value that names no
        :class:`AgentHarness` yields no override for that entry.
    *   ``phase_fanout`` — per-``(role, phase)`` fan-out opt-in (teatree#2229),
        keyed canonical ``"role:phase"`` (e.g. ``"reviewer:reviewing"``). A
        ``bool`` value enables the registry default ``fanout_n`` (``True``) or
        disables (``False``); an ``int`` value enables and overrides the panel
        width. Empty by default → ``core.phases.resolve_fanout_directive``
        renders nothing → dispatch is byte-identical to today until a pair is
        opted in. Mirrors the ``skill_models`` precedent (a typed
        ``agent_phase_fanout`` dict); the int range is validated at
        directive-render time (``core.phases._resolved_fanout_n``), fail-loud.
    """

    skill_models: dict[str, str | None] = field(default_factory=dict)
    session_model: str | None = None
    session_effort: str | None = DEFAULT_SESSION_EFFORT
    session_permission_mode: str = ""
    phase_fanout: dict[str, bool | int] = field(default_factory=dict)
    honesty_model: str = "opus"
    tier_models: dict[str, str] = field(default_factory=dict)
    pydantic_ai_tier_models: dict[str, str] = field(default_factory=dict)
    tier_effort: dict[str, str] = field(default_factory=dict)
    phase_harness: dict[str, AgentHarness | None] = field(default_factory=dict)


def _phase_fanout_from(raw: object) -> dict[str, bool | int]:
    """Normalise the ``agent_phase_fanout`` value into a ``"role:phase" → opt-in`` map.

    Each value is a ``bool`` (enable at registry default / disable) or an
    ``int`` (enable + override the panel width). ``bool`` is checked before
    ``int`` because ``bool`` is an ``int`` subclass — a bare ``true``/``false``
    must stay a bool, not collapse to ``1``/``0``. A non-dict value (a
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
        if isinstance(opt_in, bool | int):
            resolved[str(pair)] = opt_in
    return resolved


def _phase_harness_from(raw: object) -> dict[str, AgentHarness | None]:
    """Normalise the ``agent_phase_harness`` value into a ``phase → harness|unpin`` map.

    Each value is either the name of an :class:`AgentHarness` (``claude_sdk`` /
    ``pydantic_ai`` — the phase is PINNED to that transport) or an inherit
    sentinel (``""`` / ``default`` / ``inherit`` — the phase is explicitly
    UNPINNED, dropping back to the configured ``agent_harness`` even when the
    shipped :data:`teatree.agents.model_tiering.PHASE_HARNESS` default would pin
    it). The unpin case is kept as an explicit ``None`` entry (distinct from an
    absent key) so :func:`~teatree.agents.model_tiering.resolve_phase_harness`
    can tell "override to no-pin" apart from "no override at all".

    A non-dict value, a non-string entry, or a value naming no known harness is
    tolerated and skipped (mirrors :func:`_tier_models_from`), so a malformed
    override never poisons the shipped pin.
    """
    if not isinstance(raw, dict):
        return {}
    resolved: dict[str, AgentHarness | None] = {}
    for phase, value in raw.items():
        if not isinstance(value, str):
            continue
        if value.strip().lower() in _INHERIT_SENTINELS:
            resolved[str(phase)] = None
            continue
        try:
            resolved[str(phase)] = AgentHarness.parse(value)
        except ValueError:
            continue
    return resolved


def _skill_models_from(raw: object) -> dict[str, str | None]:
    """Normalise the ``agent_skill_models`` value into a floor map.

    Each value is normalised through the inherit sentinels (sentinel → ``None``).
    A non-dict value (a malformed scalar) yields an empty floor map, matching
    the ``phase_models`` loader's tolerance.
    """
    if not isinstance(raw, dict):
        return {}
    return {str(skill): _normalize_model(model) for skill, model in raw.items()}


def _tier_models_from(raw: object) -> dict[str, str]:
    """Normalise the ``agent_tier_models`` value into a tier → model-id override map.

    Each entry overrides the concrete model id a tier resolves to. A non-dict
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


def _pydantic_ai_tier_models_from(raw: object) -> dict[str, str]:
    """Normalise the ``agent_pydantic_ai_tier_models`` value into a tier → id override map.

    The ``pydantic_ai`` sibling of :func:`_tier_models_from` — identical
    tolerance (non-dict → empty, a non-string-or-blank entry skipped) so a
    malformed override never poisons the shipped
    :data:`teatree.agents.model_tiering.PYDANTIC_AI_TIER_MODELS` default.
    """
    return _tier_models_from(raw)


def _tier_effort_from(raw: object) -> dict[str, str]:
    """Normalise the ``agent_tier_effort`` value into a tier → effort override map.

    Mirrors :func:`_tier_models_from` exactly (non-dict → empty, per-entry
    tolerance), with one added gate: each value must be a member of
    :data:`EFFORT_SCALE` (case- and whitespace-insensitive). A non-string, blank,
    or off-scale value is skipped — the same fail-to-defaults tolerance — so a
    malformed override never poisons the shipped
    :data:`teatree.agents.model_tiering.TIER_EFFORT` default.
    """
    if not isinstance(raw, dict):
        return {}
    resolved: dict[str, str] = {}
    for tier, effort in raw.items():
        if isinstance(effort, str) and effort.strip().lower() in EFFORT_SCALE:
            resolved[str(tier)] = effort.strip().lower()
    return resolved


def _honesty_model_from(raw: object) -> str:
    """Normalise the ``agent_honesty_model`` value to a non-empty model id.

    Shares :func:`_normalize_model`'s boundary (whitespace strip + sentinel
    handling). An absent key or a sentinel value (which normalises to ``None``)
    falls back to ``"opus"`` — the escalation target must always be a concrete
    model id, never the inherit sentinel.
    """
    if raw is None:
        return "opus"
    return _normalize_model(raw) or "opus"


def _session_model_from(raw: object) -> str | None:
    """``None`` when absent, else the model normalised through the inherit sentinels."""
    if raw is None:
        return None
    return _normalize_model(raw)


def _session_effort_from(raw: object) -> str | None:
    """The interactive effort pin, defaulting to :data:`DEFAULT_SESSION_EFFORT`.

    Absent (``None``) → the shipped ``xhigh`` default. A present value is
    validated against :data:`EFFORT_SCALE` by :func:`parse_effort`, which raises
    fail-loud on an off-scale value (a misconfiguration the operator must see).
    """
    if raw is None:
        return DEFAULT_SESSION_EFFORT
    return parse_effort(raw)


def _session_permission_mode_from(raw: object) -> str:
    """The loop session's permission-mode override — env, then the stored value, else empty.

    Empty means no opinion: the CLI keeps its shipped pin. A non-string or blank
    stored value is likewise no opinion, so it can never pin an empty flag onto the
    ``claude`` argv.
    """
    env = os.environ.get("T3_AGENT_SESSION_PERMISSION_MODE", "").strip()
    if env:
        return env
    return raw.strip() if isinstance(raw, str) else ""


def resolve_agent_config() -> AgentConfig:
    """Resolve the effective :class:`AgentConfig` from the DB ``ConfigSetting`` store.

    Each ``[agent]`` value is read as its own DB key via
    :mod:`teatree.config.cold_reader` (``agent_session_model``,
    ``agent_skill_models``, …). A missing key yields the field's default — the
    same fail-to-defaults posture as the ``phase_models`` loader. Most default to
    empty / no pin; ``session_effort`` is the exception, defaulting to
    :data:`DEFAULT_SESSION_EFFORT` (``xhigh``) so the interactive main-agent
    session runs at high effort out of the box. An explicitly-stored *invalid*
    ``agent_session_effort`` still raises (fail loud), since that is a
    misconfiguration the user must see, not an absence to tolerate.
    """
    return AgentConfig(
        skill_models=_skill_models_from(cold_reader.read_setting("agent_skill_models")),
        session_model=_session_model_from(cold_reader.read_setting("agent_session_model")),
        session_effort=_session_effort_from(cold_reader.read_setting("agent_session_effort")),
        session_permission_mode=_session_permission_mode_from(
            cold_reader.read_setting("agent_session_permission_mode")
        ),
        phase_fanout=_phase_fanout_from(cold_reader.read_setting("agent_phase_fanout")),
        honesty_model=_honesty_model_from(cold_reader.read_setting("agent_honesty_model")),
        tier_models=_tier_models_from(cold_reader.read_setting("agent_tier_models")),
        pydantic_ai_tier_models=_pydantic_ai_tier_models_from(
            cold_reader.read_setting("agent_pydantic_ai_tier_models")
        ),
        tier_effort=_tier_effort_from(cold_reader.read_setting("agent_tier_effort")),
        phase_harness=_phase_harness_from(cold_reader.read_setting("agent_phase_harness")),
    )
