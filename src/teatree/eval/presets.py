"""Model-tier PRESETS: a composition layer over :func:`resolve_eval_model`.

A preset lets the benchmark compare model profiles, and lets a scenario be
pinned to its cheapest passing tier WITHOUT editing the scenario's own YAML
(generated corpora would clobber a hand-edited ``tier:``/``model:`` on regen).

Two shapes. A UNIFORM preset (``cheap``, ``frontier``) pins every scenario to
one tier, defined in code below. The ``baseline`` preset is file-backed — a
per-scenario tier MAP loaded from ``evals/presets/baseline.yaml`` (populated by
``t3 eval set-baseline``) — so a scenario absent from the map falls through to
its own YAML resolution rather than being silently cheapened.

:func:`resolve_preset_model` is applied at the ``run_dispatch.py`` per-scenario
seam, a layer ABOVE :func:`~teatree.eval.model_resolution.resolve_eval_model` —
it is never called from inside that function, and
``resolve_eval_model``/``resolve_tier``/``TIER_MODELS`` are untouched by this
module. Precedence with a preset active, highest first:

1.  ``spec.model`` (non-empty) — an explicit scenario pin always wins.
2.  The preset's own entry — its uniform ``tier``, or (for a per-scenario
    preset) the map entry for this scenario's name.
3.  A scenario absent from a per-scenario preset's map falls through to
    :func:`~teatree.eval.model_resolution.resolve_eval_model` — the scenario's
    own ``tier`` / ``phase`` / :data:`~teatree.agents.model_tiering.DEFAULT_TIER`.

The CLI's ``--model`` flag (which forces the WHOLE suite onto one model) sits
ABOVE this module entirely — it is mutually exclusive with ``--preset`` at the
argument-resolution seam, so it never needs to be considered here.
"""

import dataclasses
from collections.abc import Mapping
from pathlib import Path

import yaml

from teatree.agents.model_tiering import TIER_MODELS, resolve_tier
from teatree.eval.model_resolution import resolve_eval_model
from teatree.eval.models import EvalSpec

#: The checked-in baseline preset file, resolved from this module's path so the
#: eval package stays a leaf (the same convention ``discovery.SCENARIOS_DIR`` /
#: ``cost_bounds.COST_BOUNDS_PATH`` follow).
BASELINE_PRESET_PATH = Path(__file__).resolve().parents[3] / "evals" / "presets" / "baseline.yaml"

#: The preset names :func:`resolve_preset` recognises for a UNIFORM tier pin.
#: There is deliberately no "optimal" preset (YAGNI) — ``baseline`` (below) is
#: the per-scenario cheapest-tier preset, populated by ``t3 eval set-baseline``.
_CHEAP_TIER = "cheap"
_FRONTIER_TIER = "frontier"


class PresetError(RuntimeError):
    """A malformed preset — an unknown tier, a bad key, an un-approved frontier pin."""


@dataclasses.dataclass(frozen=True)
class Preset:
    """A named model-tier profile: either a UNIFORM tier or a per-scenario MAP.

    ``tier`` and ``scenario_tiers`` are mutually exclusive (enforced in
    :func:`__post_init__`). An empty ``scenario_tiers`` with no ``tier`` is a
    legitimate per-scenario preset with zero entries — every scenario falls
    through to :func:`resolve_eval_model` (the freshly-committed
    ``baseline.yaml``, before ``t3 eval set-baseline`` ever populated it).
    """

    name: str
    tier: str = ""
    scenario_tiers: Mapping[str, str] = dataclasses.field(default_factory=dict)

    def __post_init__(self) -> None:
        # An empty ``scenario_tiers`` with no ``tier`` is a legitimate per-scenario
        # preset with zero entries (the freshly-committed ``baseline.yaml``, before
        # ``t3 eval set-baseline`` ever populated it) — every scenario simply falls
        # through to its own resolution. Only BOTH set is ambiguous.
        if self.tier and self.scenario_tiers:
            msg = f"preset {self.name!r}: tier and scenario_tiers are mutually exclusive"
            raise PresetError(msg)


#: The uniform ``cheap`` preset — every scenario forced to the cheap tier.
CHEAP_PRESET = Preset(name="cheap", tier=_CHEAP_TIER)

#: The uniform ``frontier`` preset — every scenario forced to the frontier tier.
FRONTIER_PRESET = Preset(name="frontier", tier=_FRONTIER_TIER)


def resolve_preset_model(spec: EvalSpec, preset: Preset) -> str:
    """Resolve *spec*'s concrete model id under *preset* — the composition seam.

    Precedence (see the module docstring): ``spec.model`` > the preset's own
    entry > (per-scenario preset only) fall through to
    :func:`~teatree.eval.model_resolution.resolve_eval_model` — *spec*'s own
    ``tier`` / ``phase`` / default tier. A scenario absent from a per-scenario
    preset's map is NEVER silently cheapened.
    """
    if spec.model.strip():
        return spec.model
    if preset.tier:
        return resolve_tier(preset.tier)
    entry = preset.scenario_tiers.get(spec.name)
    if entry is not None:
        return resolve_tier(entry)
    return resolve_eval_model(spec)


@dataclasses.dataclass(frozen=True)
class BaselineFile:
    """The parsed+validated ``evals/presets/baseline.yaml``: tier map + frontier allow-list."""

    scenario_tiers: Mapping[str, str]
    frontier_ok: frozenset[str]


def load_baseline_file(path: Path | None = None) -> BaselineFile:
    """Load and structurally validate the baseline preset file — fail loud, never silent.

    *path* defaults to :data:`BASELINE_PRESET_PATH`, re-read from the module at
    CALL time (never bound as a function default) so a test's
    ``patch("teatree.eval.presets.BASELINE_PRESET_PATH", ...)`` is honoured.

    Every ``scenarios`` value must be a known :data:`~teatree.agents.model_tiering.TIER_MODELS`
    key (a typo'd tier is a hard error, not a silent pass-through), and a
    ``frontier``-valued entry must be listed under ``frontier_ok`` — the same
    hygiene :mod:`tests.eval_replay` pins against the checked-in file.
    """
    path = path if path is not None else BASELINE_PRESET_PATH
    if not path.is_file():
        msg = f"baseline preset file is missing: {path}"
        raise PresetError(msg)
    raw = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    if not isinstance(raw, Mapping):
        msg = f"{path}: top-level YAML must be a mapping"
        raise PresetError(msg)
    scenarios = raw.get("scenarios") or {}
    if not isinstance(scenarios, Mapping):
        msg = f"{path}: 'scenarios' must be a mapping of scenario name -> tier"
        raise PresetError(msg)
    frontier_ok_raw = raw.get("frontier_ok") or []
    if not isinstance(frontier_ok_raw, list) or not all(isinstance(item, str) for item in frontier_ok_raw):
        msg = f"{path}: 'frontier_ok' must be a list of scenario names"
        raise PresetError(msg)
    frontier_ok = frozenset(frontier_ok_raw)
    scenario_tiers: dict[str, str] = {}
    for name, tier in scenarios.items():
        if not isinstance(name, str) or not isinstance(tier, str):
            msg = f"{path}: scenario entry {name!r}: {tier!r} must be a str -> str pair"
            raise PresetError(msg)
        if tier not in TIER_MODELS:
            msg = f"{path}: scenario {name!r} declares unknown tier {tier!r}; known tiers: {sorted(TIER_MODELS)}"
            raise PresetError(msg)
        if tier == _FRONTIER_TIER and name not in frontier_ok:
            msg = f"{path}: scenario {name!r} pins the frontier tier but is not listed under 'frontier_ok'"
            raise PresetError(msg)
        scenario_tiers[name] = tier
    return BaselineFile(scenario_tiers=scenario_tiers, frontier_ok=frontier_ok)


def baseline_preset(path: Path | None = None) -> Preset:
    """The file-backed ``baseline`` preset: loads + validates ``evals/presets/baseline.yaml``.

    *path* defaults to :data:`BASELINE_PRESET_PATH`, re-read at CALL time (see
    :func:`load_baseline_file` for why the default is never bound at def time).
    """
    parsed = load_baseline_file(path)
    return Preset(name="baseline", scenario_tiers=parsed.scenario_tiers)


#: The preset names :func:`resolve_preset` recognises, resolved lazily so the
#: ``baseline`` entry re-reads ``evals/presets/baseline.yaml`` on every call —
#: the file is regenerated by ``t3 eval set-baseline`` and must never be stale.
_UNIFORM_PRESETS: Mapping[str, Preset] = {CHEAP_PRESET.name: CHEAP_PRESET, FRONTIER_PRESET.name: FRONTIER_PRESET}


def known_preset_names() -> tuple[str, ...]:
    """The preset names ``--preset``/``--presets`` accept, for CLI help/error text."""
    return (*_UNIFORM_PRESETS, "baseline")


def resolve_preset(name: str) -> Preset:
    """Resolve a preset by *name*, or raise :class:`PresetError` naming the known set."""
    if name == "baseline":
        return baseline_preset()
    if name in _UNIFORM_PRESETS:
        return _UNIFORM_PRESETS[name]
    msg = f"unknown preset {name!r}; known presets: {', '.join(known_preset_names())}"
    raise PresetError(msg)
