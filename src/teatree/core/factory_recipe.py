"""The declarative factory-score recipe loader (SIG-PR-2).

``evals/recipe.yaml`` names the SIG-PR-1 :data:`~teatree.core.factory_signals.SIGNALS`
registry ids and assigns each a weight, an optional red-floor, and — for the two
magnitude signals whose readings are not a 0..1 rate — a normalisation cap. This
module parses that committed file into typed frozen dataclasses and fails loud on
any drift, mirroring :mod:`teatree.eval.cost_bounds`'s loader doctrine: a missing
file is a configuration error (never a default recipe), and a malformed entry is a
load-time :class:`RecipeError` (never a silently-dropped weight).

The recipe is *composition by registration*, not cross-import: it names
``provider_id`` values and :func:`load_recipe` validates them against the registry,
so an unknown id or a missing one is caught before any score is computed. The
``recipe_sha`` (a sha256 over the committed file bytes) is the provenance key the
snapshot ledger stamps and ``t3 <overlay> recipe approve`` pins.
"""

import dataclasses
import hashlib
from collections.abc import Mapping
from pathlib import Path
from typing import Any

import yaml

from teatree.core.factory_signals import SIGNALS

#: The committed recipe, resolved from this module's path so ``core`` stays a leaf
#: (the same ``parents[3] / "evals"`` convention :mod:`teatree.eval.cost_bounds` uses).
RECIPE_PATH = Path(__file__).resolve().parents[3] / "evals" / "recipe.yaml"

#: The registry ids the recipe must name — exactly, no more and no fewer.
_REGISTRY_IDS: frozenset[str] = frozenset(spec.provider_id for spec in SIGNALS)

#: The magnitude signals whose readings are not a 0..1 rate (``merge_latency`` in
#: hours, ``repair_burn`` in mean iterations): each MUST carry a ``cap`` so the
#: reading normalises to 0..1. A ``cap`` on any other (rate) signal is a
#: configuration error — a rate is already bounded.
CAP_REQUIRED_IDS: frozenset[str] = frozenset({"merge_latency", "repair_burn"})

#: Weights must sum to 1.0 within this tolerance (float accumulation slack).
_WEIGHT_SUM_TOLERANCE = 1e-6


class RecipeError(ValueError):
    """A malformed or drifted ``recipe.yaml`` — caught at load time, never scored."""


@dataclasses.dataclass(frozen=True, slots=True)
class RecipeSignal:
    """One signal's recipe weighting: its ``weight``, red floor, and normalisation cap."""

    provider_id: str
    weight: float
    red_when: float | None
    cap: float | None


@dataclasses.dataclass(frozen=True, slots=True)
class Recipe:
    """The whole declarative recipe — one :class:`RecipeSignal` per registry id."""

    version: int
    coverage_floor: float
    signals: dict[str, RecipeSignal]
    recipe_sha: str

    @property
    def provider_ids(self) -> frozenset[str]:
        return frozenset(self.signals)


def recipe_sha(path: Path | None = None) -> str:
    """The sha256 hex digest over the committed recipe file's bytes.

    The provenance key: it changes on any weight/floor/cap edit, so a re-weighted
    recipe cannot silently reuse a prior sha's approval.
    """
    bounds_path = path or RECIPE_PATH
    if not bounds_path.is_file():
        msg = f"recipe file is missing: {bounds_path}. An absent recipe would make the score vacuous."
        raise RecipeError(msg)
    return hashlib.sha256(bounds_path.read_bytes()).hexdigest()


def load_recipe(path: Path | None = None) -> Recipe:
    """Parse ``recipe.yaml`` into a typed :class:`Recipe`, failing loud on drift.

    Raises :class:`RecipeError` on: a missing file (an absent recipe would make
    the score vacuously green); a malformed top-level shape; a ``coverage_floor``
    outside ``[0, 1]``; weights that do not sum to ``1.0``; an ``unknown``
    provider id or a ``missing`` registry id (the recipe must name exactly the
    SIGNALS ids); and a ``missing`` cap on a magnitude signal or a ``forbidden``
    cap on a rate signal.
    """
    recipe_path = path or RECIPE_PATH
    if not recipe_path.is_file():
        msg = f"recipe file is missing: {recipe_path}. An absent recipe would make the score vacuous."
        raise RecipeError(msg)
    loaded = yaml.safe_load(recipe_path.read_text(encoding="utf-8"))
    if not isinstance(loaded, Mapping):
        msg = f"{recipe_path}: expected a top-level mapping"
        raise RecipeError(msg)
    top: Mapping[str, Any] = {str(k): v for k, v in loaded.items()}
    version = _coerce_int(top.get("version"), path=recipe_path, field="version")
    coverage_floor = _coerce_unit(top.get("coverage_floor"), path=recipe_path, field="coverage_floor")
    signals = _parse_signals(top.get("signals"), path=recipe_path)
    return Recipe(
        version=version,
        coverage_floor=coverage_floor,
        signals=signals,
        recipe_sha=hashlib.sha256(recipe_path.read_bytes()).hexdigest(),
    )


def _parse_signals(raw: object, *, path: Path) -> dict[str, RecipeSignal]:
    if not isinstance(raw, Mapping):
        msg = f"{path}: 'signals' must be a mapping of provider_id -> weighting"
        raise RecipeError(msg)
    entries: Mapping[str, Any] = {str(k): v for k, v in raw.items()}
    named = frozenset(entries)
    unknown = named - _REGISTRY_IDS
    if unknown:
        msg = f"{path}: unknown signal id(s) not in the SIGNALS registry: {sorted(unknown)}"
        raise RecipeError(msg)
    missing = _REGISTRY_IDS - named
    if missing:
        msg = f"{path}: recipe must name every SIGNALS registry id; missing: {sorted(missing)}"
        raise RecipeError(msg)
    signals = {name: _parse_signal(name, entry, path=path) for name, entry in entries.items()}
    weight_sum = sum(sig.weight for sig in signals.values())
    if abs(weight_sum - 1.0) > _WEIGHT_SUM_TOLERANCE:
        msg = f"{path}: signal weights must sum to 1.0, got {weight_sum}"
        raise RecipeError(msg)
    return signals


def _parse_signal(name: str, entry: object, *, path: Path) -> RecipeSignal:
    if not isinstance(entry, Mapping):
        msg = f"{path}: signal {name!r} must map to a {{weight, red_when?, cap?}} mapping"
        raise RecipeError(msg)
    fields: Mapping[str, Any] = {str(k): v for k, v in entry.items()}
    if "weight" not in fields:
        msg = f"{path}: signal {name!r} is missing required 'weight'"
        raise RecipeError(msg)
    weight = _coerce_unit(fields["weight"], path=path, field=f"{name}.weight")
    red_when = _optional_float(fields, "red_when", name=name, path=path)
    cap = _optional_float(fields, "cap", name=name, path=path)
    _validate_cap(name, cap, path=path)
    return RecipeSignal(provider_id=name, weight=weight, red_when=red_when, cap=cap)


def _optional_float(fields: Mapping[str, Any], key: str, *, name: str, path: Path) -> float | None:
    raw = fields.get(key)
    return None if raw is None else _coerce_float(raw, path=path, field=f"{name}.{key}")


def _validate_cap(name: str, cap: float | None, *, path: Path) -> None:
    if name in CAP_REQUIRED_IDS:
        if cap is None:
            msg = f"{path}: magnitude signal {name!r} requires a 'cap' to normalise its reading to 0..1"
            raise RecipeError(msg)
        if cap <= 0.0:
            msg = f"{path}: signal {name!r} cap must be positive, got {cap}"
            raise RecipeError(msg)
    elif cap is not None:
        msg = f"{path}: rate signal {name!r} must not carry a 'cap' (a rate is already 0..1)"
        raise RecipeError(msg)


def _coerce_int(value: object, *, path: Path, field: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        msg = f"{path}: {field} must be an integer, got {value!r}"
        raise RecipeError(msg)
    return value


def _coerce_float(value: object, *, path: Path, field: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        msg = f"{path}: {field} must be a number, got {value!r}"
        raise RecipeError(msg)
    return float(value)


def _coerce_unit(value: object, *, path: Path, field: str) -> float:
    number = _coerce_float(value, path=path, field=field)
    if not 0.0 <= number <= 1.0:
        msg = f"{path}: {field} must be within [0, 1], got {number}"
        raise RecipeError(msg)
    return number
