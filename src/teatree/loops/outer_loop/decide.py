"""The pure keep-only-if-better decision rule (T4-PR-3).

The anti-Goodhart core: an experiment is KEPT only when its target signal improved
beyond its own ``regress_band`` AND nothing else regressed vs the admission
baseline. Optimising the target at the expense of another signal — or on noise
inside the band — is never a keep. The rule is a pure fold over the two
:class:`~teatree.core.factory_score.FactoryScore` snapshots, so it is fully
deterministic and table-tested without touching the DB.
"""

import dataclasses

from teatree.core.factory_score import FactoryScore, ScoredSignal
from teatree.core.factory_signals import SignalVerdict

_WORSE_VERDICTS = frozenset({SignalVerdict.REGRESSING.value, SignalVerdict.RED.value})


@dataclasses.dataclass(frozen=True, slots=True)
class Decision:
    """The outcome of the keep-only-if-better rule."""

    keep: bool
    reason: str


def _by_id(score: FactoryScore) -> dict[str, ScoredSignal]:
    return {sig.provider_id: sig for sig in score.signals}


def decide_keep(
    *,
    baseline: FactoryScore,
    post: FactoryScore,
    target_provider_id: str,
    regress_band: float,
) -> Decision:
    """KEEP iff the target improved beyond its band and nothing else regressed.

    An untrustworthy post score (verdict RED → ``aggregate`` is None), a target
    that did not measurably improve, or ANY non-target signal that turned
    regressing/red vs a healthy baseline all resolve to REVERT — a non-improving
    or collateral-damaging experiment is never kept.
    """
    if post.verdict == SignalVerdict.RED.value:
        return Decision(keep=False, reason="post score is RED (untrustworthy) — revert")

    baseline_by_id = _by_id(baseline)
    post_by_id = _by_id(post)

    target_before = baseline_by_id.get(target_provider_id)
    target_after = post_by_id.get(target_provider_id)
    if target_before is None or target_after is None:
        return Decision(keep=False, reason=f"target {target_provider_id!r} missing from a score — revert")
    if target_before.normalized is None or target_after.normalized is None:
        return Decision(keep=False, reason=f"target {target_provider_id!r} not covered in a score — revert")
    improvement = target_after.normalized - target_before.normalized
    if improvement <= regress_band:
        return Decision(
            keep=False,
            reason=(
                f"target {target_provider_id!r} improvement {improvement:.3f} within band {regress_band:.3f} — revert"
            ),
        )

    regressed = _collateral_regression(baseline_by_id, post_by_id, target_provider_id=target_provider_id)
    if regressed:
        return Decision(keep=False, reason=f"non-target signal {regressed!r} regressed vs baseline — revert")

    return Decision(keep=True, reason=f"target {target_provider_id!r} improved {improvement:.3f}, no regression")


def _collateral_regression(
    baseline_by_id: dict[str, ScoredSignal],
    post_by_id: dict[str, ScoredSignal],
    *,
    target_provider_id: str,
) -> str:
    """The id of the first non-target signal that turned worse vs baseline, or ``""``."""
    for provider_id, after in post_by_id.items():
        if provider_id == target_provider_id:
            continue
        before = baseline_by_id.get(provider_id)
        was_worse = before is not None and before.verdict in _WORSE_VERDICTS
        if after.verdict in _WORSE_VERDICTS and not was_worse:
            return provider_id
    return ""
