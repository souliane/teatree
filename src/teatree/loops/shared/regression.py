"""The shared anti-Goodhart collateral-regression fold.

Extracted from :mod:`teatree.loops.outer_loop.decide` so BOTH the outer loop's
keep-only-if-better rule AND the directive loop's VERIFYING step read ONE
implementation of the same question: did any factory signal turn RED/REGRESSING vs
the admission baseline? A pure fold over two
:class:`~teatree.core.factory.factory_score.FactoryScore` snapshots — no DB, table-tested.
"""

from teatree.core.factory.factory_score import FactoryScore, ScoredSignal
from teatree.core.factory.factory_signals import SignalVerdict

_WORSE_VERDICTS = frozenset({SignalVerdict.REGRESSING.value, SignalVerdict.RED.value})


def _by_id(score: FactoryScore) -> dict[str, ScoredSignal]:
    return {sig.provider_id: sig for sig in score.signals}


def no_collateral_regression(
    baseline: FactoryScore,
    post: FactoryScore,
    *,
    exclude_provider_id: str = "",
) -> str | None:
    """The id of the first signal that turned worse vs *baseline*, or ``None`` when clean.

    A signal already RED/REGRESSING in the baseline that STAYS worse is not a NEW
    collateral regression — only a signal that crossed into a worse verdict counts.
    ``exclude_provider_id`` skips the outer loop's target signal (whose own
    improvement is judged separately); the directive loop omits it, so every signal
    is treated as collateral.
    """
    baseline_by_id = _by_id(baseline)
    for provider_id, after in _by_id(post).items():
        if provider_id == exclude_provider_id:
            continue
        before = baseline_by_id.get(provider_id)
        was_worse = before is not None and before.verdict in _WORSE_VERDICTS
        if after.verdict in _WORSE_VERDICTS and not was_worse:
            return provider_id
    return None
