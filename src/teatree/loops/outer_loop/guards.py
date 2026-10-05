"""Outer-loop admission bounds on top of the shared self-improvement guards."""

from datetime import datetime

from teatree.core.models import OuterLoopExperiment
from teatree.loop.self_improve.budget import precheck_budget
from teatree.loops.shared.guards import BUDGET, SIGNAL_UNTRUSTED, GuardSeams, GuardVerdict, probe_signal_trust

CONCURRENCY_CAP = "concurrency_cap"
WEEKLY_CAP = "weekly_cap"
CONVERGED = "converged"

#: Fixed owner-approved proposal, measurement, and failure bounds.
MAX_PER_WEEK = 1
MEASURE_DAYS = 7
STOP_AFTER_CONSECUTIVE_FAILURES = 3


def evaluate_guards(
    *,
    seams: GuardSeams | None = None,
    overlay: str = "",
    now: datetime | None = None,
) -> GuardVerdict:
    """Require trustworthy signals and available budget."""
    resolved = seams or GuardSeams()
    trust = probe_signal_trust(overlay=overlay, now=now, report=resolved.signal_report)
    if not trust.trusted:
        return GuardVerdict.refuse(SIGNAL_UNTRUSTED)
    resolved_budget = resolved.budget if resolved.budget is not None else precheck_budget()
    if not resolved_budget.ok:
        return GuardVerdict.refuse(f"{BUDGET}:{resolved_budget.reason}")
    return GuardVerdict.allow()


def admission_verdict(
    *,
    overlay: str = "",
    now: datetime | None = None,
) -> GuardVerdict:
    """Whether a NEW proposal is admissible: concurrency → convergence → weekly.

    Consulted only when the tick would PROPOSE — an in-flight experiment is
    advanced regardless. ``converged`` (the human-attention brake) outranks the
    weekly cap (a mere wait): a loop that failed N times in a row should PARK and
    surface to a human, not silently idle out its weekly budget.
    """
    if OuterLoopExperiment.objects.active_count(overlay=overlay) >= 1:
        return GuardVerdict.refuse(CONCURRENCY_CAP)
    consecutive = OuterLoopExperiment.objects.consecutive_non_kept(overlay=overlay)
    if consecutive >= STOP_AFTER_CONSECUTIVE_FAILURES:
        return GuardVerdict.refuse(CONVERGED)
    if OuterLoopExperiment.objects.weekly_count(overlay=overlay, now=now) >= MAX_PER_WEEK:
        return GuardVerdict.refuse(WEEKLY_CAP)
    return GuardVerdict.allow()
