"""Reconstruct a :class:`FactoryScore` from a persisted snapshot row (north-star PR-7).

A :class:`~teatree.core.models.factory_score_snapshot.FactoryScoreSnapshot` stores a
score's signals as ``[ScoredSignal.to_dict() …]``; the directive loop's VERIFYING
step needs the admission-baseline score back as a :class:`FactoryScore` to feed the
shared :func:`~teatree.loops.shared.regression.no_collateral_regression` fold. This
is the inverse of the snapshot's own serialization — a pure mapping, no DB.
"""

from teatree.core.factory.factory_score import FactoryScore, ScoredSignal
from teatree.core.models.factory_score_snapshot import FactoryScoreSnapshot


def snapshot_to_score(snapshot: FactoryScoreSnapshot) -> FactoryScore:
    """Rebuild the :class:`FactoryScore` a snapshot captured, from its stored fields."""
    signals = [
        ScoredSignal(
            provider_id=raw["provider_id"],
            status=raw["status"],
            value=raw.get("value"),
            normalized=raw.get("normalized"),
            weight=raw.get("weight", 0.0),
            covered=raw.get("covered", False),
            red=raw.get("red", False),
            verdict=raw.get("verdict", ""),
        )
        for raw in snapshot.signals
    ]
    return FactoryScore(
        aggregate=snapshot.aggregate,
        verdict=snapshot.verdict,
        coverage=snapshot.coverage,
        coverage_floor=snapshot.coverage_floor,
        recipe_sha=snapshot.recipe_sha,
        recipe_approved=snapshot.recipe_approved,
        window_days=snapshot.window_days,
        signals=signals,
    )
