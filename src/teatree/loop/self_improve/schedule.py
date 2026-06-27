"""Cost-tier dispatcher for the self-improve monitor.

A schedule cycle runs the budget gate first; on green it iterates the
configured detectors for the requested tier and applies the action
ladder to each emitted ``DetectorReport``.

Phase 1 only ships the cheap-tier dispatcher.  Medium/expensive tiers
are accepted by the CLI surface so the env-knob contract stays stable,
but they currently resolve to an empty detector list.
"""

from collections.abc import Callable
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from teatree.loop.self_improve.actions import ActionResult, run_action_ladder
from teatree.loop.self_improve.budget import BudgetVerdict, precheck_budget
from teatree.loop.self_improve.detectors import (
    DispatchGapDetector,
    ForgottenMergeDetector,
    StaleStatuslineEntryDetector,
)
from teatree.loop.self_improve.detectors.base import DetectorReport, SelfImproveDetector

if TYPE_CHECKING:
    from teatree.core.backend_protocols import MessagingBackend


class Tier:
    """String constants for cost tiers (per BLUEPRINT § 5.7)."""

    CHEAP = "cheap"
    MEDIUM = "medium"
    EXPENSIVE = "expensive"
    ALL = "all"


def _cheap_detectors() -> list[SelfImproveDetector]:
    return [DispatchGapDetector(), ForgottenMergeDetector(), StaleStatuslineEntryDetector()]


def _medium_detectors() -> list[SelfImproveDetector]:
    # Phase 2 lives here; intentionally empty in Phase 1.
    return []


def _expensive_detectors() -> list[SelfImproveDetector]:
    # Phase 3 lives here; intentionally empty in Phase 1.
    return []


def detectors_for_tier(tier: str) -> list[SelfImproveDetector]:
    """Return the detector list for the requested tier.

    Unknown tier ⇒ empty list (the schedule cycle becomes a no-op
    rather than raising — the CLI surface stays stable when callers pass
    a typo or a future-tier name the running version does not know yet).
    """
    if tier == Tier.CHEAP:
        return _cheap_detectors()
    if tier == Tier.MEDIUM:
        return _medium_detectors()
    if tier == Tier.EXPENSIVE:
        return _expensive_detectors()
    if tier == Tier.ALL:
        return [*_cheap_detectors(), *_medium_detectors(), *_expensive_detectors()]
    return []


@dataclass(slots=True)
class TierResult:
    """One schedule cycle's outcome — for tests and the status command."""

    tier: str
    budget: BudgetVerdict
    reports: list[DetectorReport] = field(default_factory=list)
    actions: list[ActionResult] = field(default_factory=list)

    @property
    def skipped(self) -> bool:
        return not self.budget.ok


def run_tier(
    tier: str,
    *,
    messaging: "MessagingBackend | None" = None,
    detectors: list[SelfImproveDetector] | None = None,
    budget: BudgetVerdict | None = None,
    auto_fix_callable: Callable[[DetectorReport], None] | None = None,
) -> TierResult:
    """Run one schedule cycle for ``tier``.

    Tests inject an explicit ``budget`` verdict (deterministic) and a
    detector list (no real DB scan needed); production callers leave
    both ``None`` so ``precheck_budget`` and ``detectors_for_tier`` run.
    """
    verdict = budget if budget is not None else precheck_budget()
    if not verdict.ok:
        return TierResult(tier=tier, budget=verdict)
    detector_list = detectors if detectors is not None else detectors_for_tier(tier)
    reports: list[DetectorReport] = []
    actions: list[ActionResult] = []
    for detector in detector_list:
        fix = auto_fix_callable if auto_fix_callable is not None else _detector_auto_fix(detector)
        for report in detector.detect():
            reports.append(report)
            result = run_action_ladder(
                report,
                messaging=messaging,
                auto_fix_callable=fix,
            )
            if result is not None:
                actions.append(result)
    return TierResult(tier=tier, budget=verdict, reports=reports, actions=actions)


def _detector_auto_fix(detector: SelfImproveDetector) -> Callable[[DetectorReport], None] | None:
    """Adapt a detector's own ``rerender`` self-heal into the ladder callable (#2625).

    Only the whitelisted ``auto_fix=True`` detectors carry a ``rerender``; the
    action ladder still refuses to execute it unless the report opted in
    (``report.auto_fix``). A detector without ``rerender`` contributes no
    callable, so the ladder's auto-fix rung is a no-op for it. This is the
    fallback for a directly-constructed detector with no injected global seam:
    both live orchestration entry points — the dedicated ``loop_self_improve``
    slot and the tick piggyback — inject the real
    ``teatree.loop.phases.render.self_improve_rerender`` seam as the global
    ``auto_fix_callable`` instead, because a directly-constructed
    ``StaleStatuslineEntryDetector`` cannot supply it (its default ``rerender``
    is the no-op sentinel that would heal nothing).
    """
    rerender = getattr(detector, "rerender", None)
    if rerender is None:
        return None
    return lambda _report: rerender()
