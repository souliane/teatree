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

The per-skill floor is MODEL only — effort is settable session-wide (on the
interactive loop spawn) and never per-sub-agent, so there is deliberately no
``skill_effort`` axis.

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
    *   ``session_model`` — the interactive main-agent ``--model`` pin, or
        ``None`` to inherit the user's default.
    *   ``session_effort`` — the interactive main-agent ``--effort`` pin (a
        member of :data:`EFFORT_SCALE`), or ``None`` to inherit.
    """

    skill_models: dict[str, str | None] = field(default_factory=dict)
    session_model: str | None = None
    session_effort: str | None = None


def _skill_models_from(raw: object) -> dict[str, str | None]:
    """Normalise the ``[agent.skill_models]`` table into a floor map.

    Each value is normalised through the inherit sentinels (sentinel → ``None``).
    A non-table value (a malformed scalar) yields an empty floor map, matching
    the ``phase_models`` loader's tolerance.
    """
    if not isinstance(raw, dict):
        return {}
    return {str(skill): _normalize_model(model) for skill, model in raw.items()}


def _agent_config_from_table(agent: Mapping[str, object]) -> AgentConfig:
    """Build an :class:`AgentConfig` from the parsed ``[agent]`` table.

    Effort is validated here (raises on an off-scale value); model values are
    normalised through the inherit sentinels.
    """
    return AgentConfig(
        skill_models=_skill_models_from(agent.get("skill_models")),
        session_model=_normalize_model(agent["session_model"]) if "session_model" in agent else None,
        session_effort=parse_effort(agent.get("session_effort")),
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
