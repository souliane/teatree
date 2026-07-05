"""Proposal selection — the PROPOSE phase's hypothesis source (T4-PR-3).

Day-one heuristic (honest limitation, documented in BLUEPRINT): a proposal is
drafted from the worst *covered* factory signal — a RED or REGRESSING row that is
NOT an instrumentation gap (a starved signal is G3's refusal, never a target).
Operator hypotheses enter through :func:`operator_proposal`. Improving the
generated hypothesis text is post-enablement work; the selection itself is pure
and table-tested.
"""

from datetime import datetime

from teatree.core.factory_signals import FactorySignalsReport, SignalStatus, SignalVerdict, compute_factory_signals
from teatree.core.models import OuterLoopExperiment, ProposalSpec

#: Minimum normalised (0..1) improvement that counts as a real target win — a
#: heuristic day-one band separating signal from measurement noise.
DEFAULT_REGRESS_BAND = 0.02

_WORST_FIRST = (SignalVerdict.RED, SignalVerdict.REGRESSING)


def select_proposal(
    *,
    report: FactorySignalsReport | None = None,
    overlay: str = "",
    now: datetime | None = None,
) -> ProposalSpec | None:
    """Draft a proposal for the worst covered signal, or ``None`` when all healthy."""
    resolved = report if report is not None else compute_factory_signals(overlay=overlay, now=now)
    for verdict in _WORST_FIRST:
        for row in resolved.signals:
            if row.reading.status == SignalStatus.OK and row.verdict == verdict:
                return ProposalSpec(
                    hypothesis=(
                        f"Signal {row.provider_id} is {verdict.value} "
                        f"(value {row.reading.value:.3f}); propose a change to recover it."
                    ),
                    target_provider_id=row.provider_id,
                    source=OuterLoopExperiment.Source.SIGNAL_REGRESSION,
                    regress_band=DEFAULT_REGRESS_BAND,
                )
    return None


def operator_proposal(
    hypothesis: str,
    target_provider_id: str,
    *,
    regress_band: float = DEFAULT_REGRESS_BAND,
) -> ProposalSpec:
    """Wrap an operator-supplied hypothesis as a proposal spec."""
    return ProposalSpec(
        hypothesis=hypothesis,
        target_provider_id=target_provider_id,
        source=OuterLoopExperiment.Source.OPERATOR,
        regress_band=regress_band,
    )
