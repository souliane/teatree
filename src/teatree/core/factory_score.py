"""The recipe-weighted factory score (SIG-PR-2).

Folds the five SIG-PR-1 signal readings into one aggregate the T4 outer loop can
optimise against — but only ever an *honest* one. The honesty invariants are
structural, not advisory. Any signal at ``instrumentation_gap`` (a silent
recorder) or any RED report row (e.g. S4's stale-CLEAR trip) folds the whole
score to verdict RED; a recipe ``red_when`` floor tripped by a covered reading
folds to RED; an uncapped rate reading outside ``[0, 1]`` folds to RED (never
silently clamped); and coverage below ``coverage_floor`` yields
``aggregate=None`` + RED — an untrustworthy score is never a number.

A starved signal can therefore only *lower* the verdict; it can never contribute a
fabricated value. The aggregate is a number ONLY when the verdict is OK or
REGRESSING; a RED score always carries ``aggregate=None``.

:func:`score_report` is the pure fold over a given
:class:`~teatree.core.factory_signals.FactorySignalsReport` (deterministic,
unit-tested with constructed reports); :func:`score` is the DB-aware wrapper that
computes the report via the read-only ``compute_factory_signals`` seam and looks
up the snapshot deltas the outer loop diffs.
"""

import dataclasses
from datetime import datetime
from typing import Any

from teatree.core.factory_recipe import Recipe, RecipeSignal, load_recipe
from teatree.core.factory_signals import (
    DEFAULT_WINDOW_DAYS,
    Direction,
    FactorySignalsReport,
    SignalRow,
    SignalStatus,
    SignalVerdict,
    compute_factory_signals,
)
from teatree.core.models.factory_score_snapshot import FactoryScoreSnapshot


@dataclasses.dataclass(frozen=True, slots=True)
class ScoredSignal:
    """One signal's contribution to the aggregate: raw + normalised value and its red state."""

    provider_id: str
    status: str
    value: float | None
    normalized: float | None
    weight: float
    covered: bool
    red: bool
    verdict: str

    def to_dict(self) -> dict[str, Any]:
        return dataclasses.asdict(self)


@dataclasses.dataclass(frozen=True, slots=True)
class FactoryScore:
    """The recipe-weighted aggregate over the five signals, with provenance + deltas."""

    aggregate: float | None
    verdict: str
    coverage: float
    coverage_floor: float
    recipe_sha: str
    recipe_approved: bool
    window_days: int
    signals: list[ScoredSignal]
    delta_vs_previous: float | None = None
    delta_vs_last_different_recipe_sha: float | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "aggregate": self.aggregate,
            "verdict": self.verdict,
            "coverage": self.coverage,
            "coverage_floor": self.coverage_floor,
            "recipe_sha": self.recipe_sha,
            "recipe_approved": self.recipe_approved,
            "window_days": self.window_days,
            "delta_vs_previous": self.delta_vs_previous,
            "delta_vs_last_different_recipe_sha": self.delta_vs_last_different_recipe_sha,
            "signals": [sig.to_dict() for sig in self.signals],
        }


def _normalize(value: float, direction: Direction, cap: float | None) -> tuple[float, bool]:
    """Map a reading to ``0..1`` (higher = healthier). Returns ``(normalized, in_range)``.

    A capped magnitude signal scales by its cap and clamps — a reading above the cap
    is fully bad (``0.0``), always in range. An uncapped rate must already be in
    ``[0, 1]``; a reading outside it is ``in_range=False`` so the caller folds it RED
    rather than clamp.
    """
    if cap is not None:
        # The recipe loader permits a cap ONLY on the LOWER_IS_BETTER magnitude
        # signals (merge_latency, repair_burn), so a capped reading always
        # normalises as "closer to zero is healthier", clamped to 0..1.
        return max(0.0, 1.0 - value / cap), True
    in_range = 0.0 <= value <= 1.0
    normalized = value if direction == Direction.HIGHER_IS_BETTER else 1.0 - value
    return normalized, in_range


def _floor_tripped(value: float, direction: Direction, red_when: float | None) -> bool:
    if red_when is None:
        return False
    if direction == Direction.HIGHER_IS_BETTER:
        return value <= red_when
    return value >= red_when


def _score_one(row: SignalRow, rsig: RecipeSignal) -> tuple[ScoredSignal, bool, float | None]:
    """Fold one report row + its recipe entry into a :class:`ScoredSignal`.

    Returns the scored signal, whether it trips the whole score RED, and — when it
    is a covered, non-red contributor — its normalised value for the weighted sum
    (else ``None``). A signal is ``covered`` iff it produced an ``ok`` reading; a
    covered reading that trips a floor or falls out of range is covered AND red.
    """
    reading = row.reading
    status = reading.status
    value: float | None = None
    normalized: float | None = None
    covered = False
    red = False
    contribution: float | None = None
    if status == SignalStatus.INSTRUMENTATION_GAP or row.verdict == SignalVerdict.RED:
        red = True
    elif status == SignalStatus.OK:
        covered = True
        value = reading.value
        normalized, in_range = _normalize(reading.value, row.direction, rsig.cap)
        if not in_range:
            red, normalized = True, None
        elif _floor_tripped(reading.value, row.direction, rsig.red_when):
            red = True
        else:
            contribution = normalized
    scored = ScoredSignal(
        provider_id=rsig.provider_id,
        status=status.value,
        value=value,
        normalized=normalized,
        weight=rsig.weight,
        covered=covered,
        red=red,
        verdict=row.verdict.value,
    )
    return scored, red, contribution


def score_report(
    recipe: Recipe,
    report: FactorySignalsReport,
    *,
    approved_recipe_sha: str = "",
) -> FactoryScore:
    """The pure recipe fold over a computed signals *report* (no DB, no deltas).

    Composes worst-wins honesty (see the module docstring) into a
    :class:`FactoryScore`. ``recipe_approved`` is stamped by comparing the
    recipe's ``recipe_sha`` to *approved_recipe_sha* whole — a mismatch (the
    shipped state until a human runs ``t3 <overlay> recipe approve``) stamps ``False``.
    Deltas are left ``None``; :func:`score` fills them from the snapshot ledger.
    """
    rows = {row.provider_id: row for row in report.signals}
    scored: list[ScoredSignal] = []
    contributions: list[tuple[float, float]] = []
    any_red = False
    any_regressing = False
    for provider_id, rsig in recipe.signals.items():
        row = rows[provider_id]
        one, red, contribution = _score_one(row, rsig)
        scored.append(one)
        any_red = any_red or red
        if row.verdict == SignalVerdict.REGRESSING:
            any_regressing = True
        if contribution is not None:
            contributions.append((rsig.weight, contribution))
    total = len(recipe.signals)
    covered = sum(1 for sig in scored if sig.covered)
    coverage = covered / total if total else 0.0
    coverage_ok = coverage >= recipe.coverage_floor
    red = any_red or not coverage_ok
    aggregate = None if red else _weighted(contributions)
    verdict = SignalVerdict.RED if red else (SignalVerdict.REGRESSING if any_regressing else SignalVerdict.OK)
    return FactoryScore(
        aggregate=aggregate,
        verdict=verdict.value,
        coverage=coverage,
        coverage_floor=recipe.coverage_floor,
        recipe_sha=recipe.recipe_sha,
        recipe_approved=bool(approved_recipe_sha) and recipe.recipe_sha == approved_recipe_sha,
        window_days=report.window_days,
        signals=scored,
    )


def _weighted(contributions: list[tuple[float, float]]) -> float | None:
    """Weighted mean of ``(weight, normalized)`` pairs, renormalised over the covered set."""
    total_weight = sum(weight for weight, _ in contributions)
    if total_weight <= 0.0:
        return None
    return sum(weight * value for weight, value in contributions) / total_weight


def score(
    *,
    recipe: Recipe | None = None,
    window_days: int = DEFAULT_WINDOW_DAYS,
    overlay: str = "",
    now: datetime | None = None,
    approved_recipe_sha: str = "",
) -> FactoryScore:
    """Compute the recipe-weighted score over the trailing window, with snapshot deltas.

    Reads the signals through the SIG-PR-1 ``compute_factory_signals`` seam
    (read-only), folds via :func:`score_report`, then fills
    ``delta_vs_previous`` and ``delta_vs_last_different_recipe_sha`` from the
    :class:`~teatree.core.models.factory_score_snapshot.FactoryScoreSnapshot`
    ledger — so a recipe edit (which changes ``recipe_sha``) cannot hide a
    regression behind its own re-weighting. A delta is ``None`` when either side
    has no numeric aggregate.
    """
    resolved_recipe = recipe or load_recipe()
    report = compute_factory_signals(window_days=window_days, overlay=overlay, now=now)
    base = score_report(resolved_recipe, report, approved_recipe_sha=approved_recipe_sha)
    previous = FactoryScoreSnapshot.objects.previous(overlay=overlay)
    last_diff = FactoryScoreSnapshot.objects.last_with_different_recipe_sha(
        resolved_recipe.recipe_sha,
        overlay=overlay,
    )
    return dataclasses.replace(
        base,
        delta_vs_previous=_delta(base.aggregate, previous),
        delta_vs_last_different_recipe_sha=_delta(base.aggregate, last_diff),
    )


def _delta(aggregate: float | None, snapshot: FactoryScoreSnapshot | None) -> float | None:
    if aggregate is None or snapshot is None or snapshot.aggregate is None:
        return None
    return aggregate - snapshot.aggregate
