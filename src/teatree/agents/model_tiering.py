"""Per-phase headless model tiering (#880, #562 §3).

Headless tasks otherwise inherit the user's default Claude model (typically
Opus). Per the Effective-Tokens formula in #562, Opus costs ~5x Sonnet and
~20x Haiku per token. Mechanical phases (review, test, ship, retro) do not
need full reasoning, so this module resolves a cheaper model tier for them
while leaving judgment phases (coding, debugging) on the user's default.

The mapping is config-driven via ``~/.teatree.toml``::

    [agent]
    phase_models.reviewing = "opus"   # pin a phase back to the reasoning tier
    phase_models.coding = "sonnet"    # opt a reasoning phase into a cheap tier
    phase_models.testing = ""         # opt out — inherit the user's default

The shipped default (:data:`DEFAULT_PHASE_MODELS`) is conservative: it only
downgrades phases that are mechanical by nature. Phases absent from the map
(and reasoning phases) return ``None`` so no ``--model`` flag is added and
the user's configured default applies unchanged.
"""

import tomllib
from pathlib import Path

from teatree.config import CONFIG_PATH

# Conservative default phase -> model-tier mapping. Only phases that are
# mechanical by nature are downgraded; coding and debugging are deliberately
# absent so they keep the user's full-reasoning default model.
DEFAULT_PHASE_MODELS: dict[str, str] = {
    "planning": "opus",
    "reviewing": "sonnet",
    "requesting_review": "sonnet",
    "testing": "sonnet",
    "shipping": "sonnet",
    "retrospecting": "haiku",
}

# Values that explicitly opt a phase out of tiering — the resolver returns
# ``None`` for these so the user's default model is inherited (no flag).
_INHERIT_SENTINELS = frozenset({"", "default", "inherit"})


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
