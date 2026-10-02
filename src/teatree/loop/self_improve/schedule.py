"""Cost-tier dispatcher for the self-improve monitor.

A schedule cycle runs the budget gate first; on green it iterates the
configured detectors for the requested tier and applies the action
ladder to each emitted ``DetectorReport``.

Only the cheap tier has detectors (BLUEPRINT § 5.7).  The medium and
expensive sets sketched in the § 5.7 plan were never built, so both —
and any unknown tier name — are REFUSED with ``UnimplementedTierError``
rather than resolving to an empty detector list: a cycle that scans
nothing and reports success is a worse contract than a loud refusal.
"""

from collections.abc import Callable
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from django.utils import timezone

from teatree.core.telemetry.admission import record_factory_issue
from teatree.loop.scanners.clear_stall_lookup import live_pr_state_reader
from teatree.loop.self_improve.actions import ActionResult, run_action_ladder
from teatree.loop.self_improve.budget import (
    DEFAULT_SPAWN_CAP_WINDOW_SECONDS,
    BudgetVerdict,
    precheck_budget,
    recent_self_improve_firings,
)
from teatree.loop.self_improve.detectors import (
    DispatchGapDetector,
    ForgottenMergeDetector,
    LifecycleIncidentDetector,
    PressureIncidentDetector,
    SkillAssuranceGapDetector,
    StaleStatuslineEntryDetector,
    TelemetryActionGapDetector,
)
from teatree.loop.self_improve.detectors.base import (
    ConfidenceAwareDetector,
    DetectorReport,
    DetectorScan,
    SelfImproveDetector,
)
from teatree.loop.self_improve.persistence import resolve_absent_firings

if TYPE_CHECKING:
    from teatree.core.backend_protocols import MessagingBackend
    from teatree.core.models.self_improve_firing import SelfImproveFiring


class Tier:
    """String constants for cost tiers (per BLUEPRINT § 5.7)."""

    CHEAP = "cheap"
    MEDIUM = "medium"
    EXPENSIVE = "expensive"
    ALL = "all"


IMPLEMENTED_TIERS: tuple[str, ...] = (Tier.CHEAP, Tier.ALL)
UNBUILT_TIERS: tuple[str, ...] = (Tier.MEDIUM, Tier.EXPENSIVE)


def _cheap_detectors(*, overlay_name: str | None = None) -> list[SelfImproveDetector]:
    # The forge reader is injected here rather than defaulted inside the detector:
    # its fail-safe default reports nothing, so this is the one place that arms it.
    return [
        DispatchGapDetector(),
        ForgottenMergeDetector(read_state=live_pr_state_reader()),
        LifecycleIncidentDetector(overlay_name=overlay_name),
        PressureIncidentDetector(),
        SkillAssuranceGapDetector(overlay_name=overlay_name),
        StaleStatuslineEntryDetector(),
        TelemetryActionGapDetector(),
    ]


def _refusal_message(tier: str) -> str:
    shipped = ", ".join(d.name for d in _cheap_detectors())
    if tier in UNBUILT_TIERS:
        return (
            f"self-improve tier {tier!r} has no detectors — the medium/expensive sets sketched in "
            f"the § 5.7 plan (souliane/teatree#979) were never built. Shipped detectors "
            f"({Tier.CHEAP} tier): {shipped}. Run --tier {Tier.CHEAP} or --tier {Tier.ALL}."
        )
    return (
        f"unknown self-improve tier {tier!r}. Implemented tiers: {', '.join(IMPLEMENTED_TIERS)} "
        f"({', '.join(UNBUILT_TIERS)} have no detectors and are refused)."
    )


class UnimplementedTierError(ValueError):
    def __init__(self, tier: str) -> None:
        super().__init__(_refusal_message(tier))
        self.tier = tier


def require_implemented_tier(tier: str) -> None:
    """Raise unless ``tier`` has detectors — the CLI surfaces' shared refusal seam."""
    if tier not in IMPLEMENTED_TIERS:
        raise UnimplementedTierError(tier)


def detectors_for_tier(tier: str, *, overlay_name: str | None = None) -> list[SelfImproveDetector]:
    """Return the detector list for the requested tier.

    A tier with no detectors raises rather than returning an empty list —
    see the module docstring.
    """
    require_implemented_tier(tier)
    return _cheap_detectors(overlay_name=overlay_name)


@dataclass(slots=True)
class TierResult:
    """One schedule cycle's outcome — for tests and the status command."""

    tier: str
    budget: BudgetVerdict
    reports: list[DetectorReport] = field(default_factory=list)
    actions: list[ActionResult] = field(default_factory=list)
    degraded_scans: list[tuple[str, str]] = field(default_factory=list)

    @property
    def skipped(self) -> bool:
        return not self.budget.ok


@dataclass(frozen=True, slots=True)
class DeliveryRoutes:
    """One injected delivery boundary for alerts and repair ownership."""

    messaging: "MessagingBackend | None" = None
    owner_alert: Callable[[DetectorReport, "SelfImproveFiring | None"], bool] | None = None
    overlay_name: str | None = None


def run_tier(
    tier: str,
    *,
    delivery: DeliveryRoutes | None = None,
    detectors: list[SelfImproveDetector] | None = None,
    budget: BudgetVerdict | None = None,
    auto_fix_callable: Callable[[DetectorReport], None] | None = None,
) -> TierResult:
    """Run one schedule cycle for ``tier``.

    Tests inject an explicit ``budget`` verdict (deterministic) and a
    detector list (no real DB scan needed); production callers leave
    both ``None`` so ``precheck_budget`` and ``detectors_for_tier`` run.

    The tier resolves before the budget gate so an unbuilt tier is
    refused even on a red budget — a skip verdict must never mask the
    refusal behind a benign "skipped" result.
    """
    routes = delivery or DeliveryRoutes()
    detector_list = detectors if detectors is not None else detectors_for_tier(tier, overlay_name=routes.overlay_name)
    verdict = (
        budget
        if budget is not None
        else precheck_budget(recent_self_improve_spawns=recent_self_improve_firings(DEFAULT_SPAWN_CAP_WINDOW_SECONDS))
    )
    reports: list[DetectorReport] = []
    actions: list[ActionResult] = []
    degraded_scans: list[tuple[str, str]] = []
    for detector in detector_list:
        if not verdict.ok and not getattr(detector, "always_on", False):
            continue
        observed_at = timezone.now()
        scan = (
            detector.detect_checked()
            if isinstance(detector, ConfidenceAwareDetector)
            else DetectorScan(detector.detect())
        )
        detected = scan.reports
        if not scan.complete:
            degraded_scans.append((detector.name, scan.reason or "source_unknown"))
        resolve_absent_firings(
            detector.name,
            scan,
            observed_at=observed_at,
            key_prefix=getattr(detector, "dedup_prefix", None),
        )
        fix = auto_fix_callable if auto_fix_callable is not None else _detector_auto_fix(detector)
        for report in detected:
            reports.append(report)
            record_factory_issue(report)
            if not verdict.ok and report.auto_fix:
                continue
            result = run_action_ladder(
                report,
                overlay_name=routes.overlay_name,
                messaging=routes.messaging,
                owner_alert=routes.owner_alert,
                auto_fix_callable=fix if verdict.ok else None,
            )
            if result is not None:
                actions.append(result)
    return TierResult(tier=tier, budget=verdict, reports=reports, actions=actions, degraded_scans=degraded_scans)


def _detector_auto_fix(detector: SelfImproveDetector) -> Callable[[DetectorReport], None] | None:
    """Adapt a detector's own ``rerender`` self-heal into the ladder callable (#2625).

    Only the whitelisted ``auto_fix=True`` detectors carry a ``rerender``; the
    action ladder still refuses to execute it unless the report opted in
    (``report.auto_fix``). A detector without ``rerender`` contributes no
    callable, so the ladder's auto-fix rung is a no-op for it. This is the
    fallback for a directly-constructed detector with no injected global seam:
    the live orchestration entry point — the dedicated ``loop_self_improve``
    slot — injects the real
    ``teatree.loop.phases.render.self_improve_rerender`` seam as the global
    ``auto_fix_callable`` instead, because a directly-constructed
    ``StaleStatuslineEntryDetector`` cannot supply it (its default ``rerender``
    is the no-op sentinel that would heal nothing).
    """
    rerender = getattr(detector, "rerender", None)
    if rerender is None:
        return None
    return lambda _report: rerender()
