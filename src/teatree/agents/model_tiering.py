"""Per-phase headless model tiering (#880, #562 §3).

Headless tasks otherwise inherit the user's default Claude model (typically
Opus). Per the Effective-Tokens formula in #562, Opus costs ~5x Sonnet and
~20x Haiku per token. This module pins and downgrades phase model tiers:
planning is pinned UP to opus as a structural floor (it requires full
reasoning); mechanical phases (review, test, ship, retro) are downgraded to
sonnet or haiku; judgment phases (coding, debugging) are left absent so they
inherit the user's default unchanged.

The mapping is config-driven via ``~/.teatree.toml``::

    [agent]
    phase_models.reviewing = "opus"   # pin a phase back to the reasoning tier
    phase_models.coding = "sonnet"    # opt a reasoning phase into a cheap tier
    phase_models.testing = ""         # opt out — inherit the user's default

Phases absent from :data:`DEFAULT_PHASE_MODELS` return ``None`` so no
``--model`` flag is added and the user's configured default applies unchanged.
"""

import tomllib
from collections.abc import Iterable
from pathlib import Path

from teatree.config import CONFIG_PATH
from teatree.config_agent import _INHERIT_SENTINELS, resolve_agent_config
from teatree.core.cost import tier_rank

# Default phase -> model-tier mapping. planning is pinned UP to opus as a
# structural floor; mechanical phases are downgraded to sonnet/haiku; coding
# and debugging are absent so they keep the user's full-reasoning default.
DEFAULT_PHASE_MODELS: dict[str, str] = {
    "planning": "opus",
    "reviewing": "sonnet",
    "requesting_review": "sonnet",
    "testing": "sonnet",
    "shipping": "sonnet",
    "retrospecting": "haiku",
}


def resolve_phase_model(phase: str, *, config_path: Path | None = None) -> str | None:
    """Resolve the Claude model tier for *phase*.

    Resolution order, first match wins:
    a config override in ``[agent] phase_models.<phase>`` of
    ``~/.teatree.toml`` (a sentinel value — empty / ``"default"`` /
    ``"inherit"`` — disables tiering for that phase); else the
    conservative :data:`DEFAULT_PHASE_MODELS` shipped default; else
    ``None``, meaning the phase is unmapped (or a reasoning phase) so the
    caller must not pass ``--model`` and the user's default model applies.
    """
    overrides = _load_phase_model_overrides(config_path)
    if phase in overrides:
        value = overrides[phase].strip()
        if value.lower() in _INHERIT_SENTINELS:
            return None
        return value
    return DEFAULT_PHASE_MODELS.get(phase)


def resolve_spawn_model(phase: str, *, skills: Iterable[str], config_path: Path | None = None) -> str | None:
    """Resolve the spawn model: the phase model raised by the per-skill floors.

    Starts from :func:`resolve_phase_model` (the per-phase tier) and merges in
    the ``[agent.skill_models]`` MODEL floor of every loaded skill in *skills*.
    The merge is *most-capable-wins*: a floor can only RAISE the resulting
    model's capability (via :func:`teatree.core.cost.tier_rank`), never lower
    it, so the merge is order-independent. A skill with no floor entry, or one
    whose floor is an inherit sentinel (``None`` after normalisation),
    contributes nothing.

    Returns ``None`` when the phase inherits AND no skill floor applies — the
    caller then passes no ``--model`` and the user's default model applies, so
    absent config is byte-for-byte the prior :func:`resolve_phase_model`
    behaviour. MODEL only: there is no per-skill effort axis (effort is a
    session-wide pin set on the interactive loop spawn).
    """
    winner = resolve_phase_model(phase, config_path=config_path)
    skill_models = resolve_agent_config(config_path=config_path).skill_models
    for skill in skills:
        floor = skill_models.get(skill)
        if floor is not None and tier_rank(floor) > tier_rank(winner):
            winner = floor
    return winner


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
