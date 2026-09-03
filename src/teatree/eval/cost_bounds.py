"""Declarative per-scenario metered-cost ceilings — the absolute cost gate.

The relative ``--gate-cost-regression`` gate (``cli/eval/run_modes.py``) diffs a
run against a *mutable DB baseline run* and no-ops a zero-cost scenario. This
module is its absolute, checked-in counterpart: a flat ``evals/cost_bounds.yaml``
fixes each scenario's CALIBRATED ``bound_usd`` (from the uncapped baseline
calibration) plus a tolerance ``margin``, and :func:`check_cost_bounds` fails a
metered run loud when any scenario's recorded ``cost_usd`` exceeds
``bound_usd * (1 + margin)``.

Two violation kinds, both RED:

* ``OVER`` — the scenario ran and its cost rose above its ceiling.
* ``MISSING`` — the scenario is *configured* but the run recorded no cost for it
(it did not execute, or metered ``$0``). A configured scenario silently costing
nothing is fail-loud, never skip-as-pass — that is the whole point of pinning it.

A scenario absent from the config is un-bounded (the run carries scenarios the
config does not pin). The config is checked in, so the ceiling survives a DB
reset and every change is reviewed in a diff.

A ceiling set that pins NO scenario is the third RED. It binds nothing, so the
gate would report a green it never earned — the identical vacuity
:func:`load_cost_bounds` already refuses for an ABSENT file, which an EMPTY one
otherwise walks straight through.

All three REDs presuppose a backend that RECORDS cost — ``--backend api`` alone.
Every backend in :data:`~teatree.eval.backends.UNMETERED_FRESH_BACKENDS` reports
none, so a healthy run there records ``$0`` and every ceiling below reads
``MISSING``: the gate is unsatisfiable in both directions and a calibration makes it
worse. That pairing is refused at the CLI boundary
(:func:`~teatree.cli.eval.app_helpers.require_metering_backend_for_cost_bounds`)
rather than handled here, so no run is billed to produce violations that cannot mean
what they say.
"""

import dataclasses
import enum
import math
from collections.abc import Mapping
from pathlib import Path
from typing import Any

import yaml

#: The committed ceilings file, resolved from this module's path so the eval
#: package stays a leaf (the same convention ``discovery.SCENARIOS_DIR`` follows).
COST_BOUNDS_PATH = Path(__file__).resolve().parents[3] / "evals" / "cost_bounds.yaml"

#: Applied to a scenario that pins a ``bound_usd`` but omits its own ``margin``.
_FALLBACK_DEFAULT_MARGIN = 0.25

#: Rendered when the ceiling set bound nothing — a requested gate that checked zero
#: scenarios is RED, never skip-as-pass.
VACUOUS_RENDER = (
    "COST BOUNDS VACUOUS: evals/cost_bounds.yaml pins no scenarios, so --gate-cost-bounds "
    "bound nothing and any green it reports is unearned. Pin a calibrated bound_usd for at "
    "least one scenario, or drop --gate-cost-bounds from the lane."
)


class CostBoundsError(ValueError):
    """A malformed ``cost_bounds.yaml`` — a typo'd key, a non-numeric bound, etc."""


@dataclasses.dataclass(frozen=True, slots=True)
class ScenarioCostBound:
    """One scenario's calibrated ceiling: ``bound_usd`` lifted by ``margin``."""

    scenario_name: str
    bound_usd: float
    margin: float

    @property
    def ceiling_usd(self) -> float:
        """The recorded cost may reach this before the gate flags the scenario."""
        return self.bound_usd * (1.0 + self.margin)


@dataclasses.dataclass(frozen=True, slots=True)
class CostBoundsConfig:
    """The whole declarative ceiling set — one :class:`ScenarioCostBound` per pinned scenario."""

    default_margin: float
    bounds: dict[str, ScenarioCostBound]

    @property
    def scenario_names(self) -> frozenset[str]:
        return frozenset(self.bounds)


class CostBoundViolationKind(enum.Enum):
    OVER = "over"
    MISSING = "missing"


@dataclasses.dataclass(frozen=True, slots=True)
class CostBoundViolation:
    """One scenario that failed its ceiling — over budget, or configured-but-uncosted."""

    scenario_name: str
    kind: CostBoundViolationKind
    bound: ScenarioCostBound
    recorded_cost_usd: float | None

    def render(self) -> str:
        if self.kind is CostBoundViolationKind.MISSING:
            return (
                f"COST MISSING {self.scenario_name}: configured (bound "
                f"${self.bound.bound_usd:.4f}) but the run recorded no cost — "
                "did it execute / meter $0?"
            )
        recorded = self.recorded_cost_usd if self.recorded_cost_usd is not None else 0.0
        return (
            f"COST OVER BOUND {self.scenario_name}: ${recorded:.4f} > "
            f"${self.bound.ceiling_usd:.4f} (bound ${self.bound.bound_usd:.4f} "
            f"+{self.bound.margin:.0%} margin)"
        )


@dataclasses.dataclass(frozen=True, slots=True)
class CostBoundsResult:
    """The outcome of checking a run's recorded costs against the ceilings."""

    violations: list[CostBoundViolation]
    checked: int

    @property
    def vacuous(self) -> bool:
        """No ceiling bound anything, so this check earned no verdict to report."""
        return self.checked == 0

    @property
    def failed(self) -> bool:
        return self.vacuous or bool(self.violations)

    def render_failures(self) -> list[str]:
        """Every line the gate prints — per-scenario violations, then vacuity if it bound nothing."""
        lines = [violation.render() for violation in self.violations]
        if self.vacuous:
            lines.append(VACUOUS_RENDER)
        return lines


def load_cost_bounds(path: Path | None = None) -> CostBoundsConfig:
    """Parse ``cost_bounds.yaml`` into a typed :class:`CostBoundsConfig`.

    Raises :class:`CostBoundsError` on a malformed file (so a typo'd ceiling is a
    hard RED at gate time, never a silently-dropped bound), and on a missing file —
    an absent ceiling set would make the gate vacuously green. An EMPTY file parses
    clean here (reading the current ceilings is not gating them); the same vacuity is
    caught at gate time by :attr:`CostBoundsResult.vacuous`.
    """
    bounds_path = path or COST_BOUNDS_PATH
    if not bounds_path.is_file():
        msg = (
            f"cost-bounds file is missing: {bounds_path}. An absent file would make the "
            "declarative cost gate vacuously green. Check the path / the move."
        )
        raise CostBoundsError(msg)
    loaded = yaml.safe_load(bounds_path.read_text(encoding="utf-8"))
    if not isinstance(loaded, Mapping):
        msg = f"{bounds_path}: expected a top-level mapping"
        raise CostBoundsError(msg)
    top: Mapping[str, Any] = {str(k): v for k, v in loaded.items()}
    default_margin = _coerce_margin(top.get("default_margin", _FALLBACK_DEFAULT_MARGIN), bounds_path)
    raw_scenarios = top.get("scenarios") or {}
    if not isinstance(raw_scenarios, Mapping):
        msg = f"{bounds_path}: 'scenarios' must be a mapping of name -> bound"
        raise CostBoundsError(msg)
    bounds = {
        str(name): _parse_bound(str(name), entry, default_margin=default_margin, path=bounds_path)
        for name, entry in raw_scenarios.items()
    }
    return CostBoundsConfig(default_margin=default_margin, bounds=bounds)


def _parse_bound(name: str, entry: object, *, default_margin: float, path: Path) -> ScenarioCostBound:
    if not isinstance(entry, Mapping):
        msg = f"{path}: scenario {name!r} must map to a {{bound_usd, margin?}} mapping"
        raise CostBoundsError(msg)
    bound_map: Mapping[str, Any] = {str(k): v for k, v in entry.items()}
    if "bound_usd" not in bound_map:
        msg = f"{path}: scenario {name!r} is missing required 'bound_usd'"
        raise CostBoundsError(msg)
    bound_usd = _coerce_float(bound_map["bound_usd"], path=path, field=f"{name}.bound_usd")
    if bound_usd < 0.0:
        msg = f"{path}: scenario {name!r} bound_usd must be non-negative"
        raise CostBoundsError(msg)
    raw_margin = bound_map.get("margin")
    margin = default_margin if raw_margin is None else _coerce_margin(raw_margin, path)
    return ScenarioCostBound(scenario_name=name, bound_usd=bound_usd, margin=margin)


def _coerce_margin(value: object, path: Path) -> float:
    margin = _coerce_float(value, path=path, field="margin")
    if margin < 0.0:
        msg = f"{path}: margin must be non-negative, got {margin}"
        raise CostBoundsError(msg)
    return margin


def _coerce_float(value: object, *, path: Path, field: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        msg = f"{path}: {field} must be a number, got {value!r}"
        raise CostBoundsError(msg)
    number = float(value)
    # YAML's `.inf` / `.nan` parse to floats no recorded cost can ever exceed, so
    # accepting one disarms this scenario's ceiling while the gate still reads green.
    if not math.isfinite(number):
        msg = f"{path}: {field} must be a finite number, got {value!r}"
        raise CostBoundsError(msg)
    return number


def check_cost_bounds(recorded_costs: dict[str, float], config: CostBoundsConfig) -> CostBoundsResult:
    """Check a run's per-scenario ``recorded_costs`` against the calibrated ceilings.

    For every *configured* scenario:

    * absent from ``recorded_costs`` (or recorded ``$0``) → a ``MISSING`` violation (fail-loud);
    * recorded above ``bound_usd * (1 + margin)`` → an ``OVER`` violation;
    * recorded within the ceiling → no violation.

    Scenarios present in the run but absent from the config are un-bounded and
    ignored. The result's :attr:`~CostBoundsResult.failed` is ``True`` when any
    violation exists, AND when the config pinned no scenario at all
    (:attr:`~CostBoundsResult.vacuous`) — a check that bound nothing has no green to
    report. A run sharing NO scenario with a non-empty config needs no special case:
    every configured ceiling is then ``MISSING``, which is already RED.
    """
    violations: list[CostBoundViolation] = []
    for name in sorted(config.scenario_names):
        bound = config.bounds[name]
        recorded = recorded_costs.get(name)
        if recorded is None or recorded <= 0.0:
            violations.append(
                CostBoundViolation(
                    scenario_name=name,
                    kind=CostBoundViolationKind.MISSING,
                    bound=bound,
                    recorded_cost_usd=recorded,
                )
            )
        elif recorded > bound.ceiling_usd:
            violations.append(
                CostBoundViolation(
                    scenario_name=name,
                    kind=CostBoundViolationKind.OVER,
                    bound=bound,
                    recorded_cost_usd=recorded,
                )
            )
    return CostBoundsResult(violations=violations, checked=len(config.bounds))
