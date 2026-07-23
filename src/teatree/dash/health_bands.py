"""The four health-view bands, each composed from an existing read-only reader (#3162).

Nothing here computes health from scratch — the verdict is ``read_health``, the
loop table is ``loops.live.build_report``, capacity reads the same parked-lane /
token-usage / cost / queue selectors the statusline uses, and the mode band reads
``mode_resolution.resolve_active_mode`` plus the ``danger_gate_fail_open`` switch. Sharing
the data layer is what keeps this page from drifting from the statusline (the
#1172 duplication objection). Every band is fail-open on its own so one broken
read degrades that band, never the whole page.
"""

import logging
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime

from django.core.cache import cache
from django.utils import timezone

from teatree.config import get_effective_settings
from teatree.core.cost import CostReport, cycle_start, cycle_start_datetime
from teatree.core.factory.operational_health import read_health
from teatree.core.mode_resolution import resolve_active_mode
from teatree.core.models.anthropic_token_usage import AnthropicTokenUsage
from teatree.core.models.known_issue import KnownIssue
from teatree.core.models.task_attempt import TaskAttempt
from teatree.core.models.usage_window_state import UsageWindowState
from teatree.core.selectors import build_headless_queue, build_interactive_queue
from teatree.dash.gate_state import dash_gate_fail_open
from teatree.loops.live import LoopStatusReport, build_report

logger = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class VerdictBand:
    status: str
    open_issues: tuple[KnownIssue, ...]
    error: str | None = None

    @classmethod
    def degraded(cls, message: str) -> "VerdictBand":
        return cls(status="error", open_issues=(), error=message)


@dataclass(frozen=True, slots=True)
class LoopsBand:
    report: LoopStatusReport | None
    worker_dead: bool
    error: str | None = None

    @classmethod
    def degraded(cls, message: str) -> "LoopsBand":
        return cls(report=None, worker_dead=False, error=message)


@dataclass(frozen=True, slots=True)
class ParkedLane:
    lane: str
    cause: str
    resets_at: datetime | None
    detected_at: datetime


@dataclass(frozen=True, slots=True)
class AccountUtilization:
    pass_path: str
    utilization_5h: float
    utilization_7d: float
    is_exhausted: bool
    earliest_reset: datetime | None


@dataclass(frozen=True, slots=True)
class SpendSummary:
    chip: str
    cycle_to_date_usd: float
    credit_usd: float
    projected_month_end_usd: float


@dataclass(frozen=True, slots=True)
class QueueDepth:
    headless: int
    interactive: int


@dataclass(frozen=True, slots=True)
class CapacityBand:
    parked_lanes: tuple[ParkedLane, ...] = ()
    accounts: tuple[AccountUtilization, ...] = ()
    spend: SpendSummary | None = None
    queue: QueueDepth = field(default_factory=lambda: QueueDepth(headless=0, interactive=0))
    error: str | None = None

    @classmethod
    def degraded(cls, message: str) -> "CapacityBand":
        return cls(error=message)


@dataclass(frozen=True, slots=True)
class ModeBand:
    mode: str
    source: str
    gate_fail_open: bool
    error: str | None = None

    @classmethod
    def degraded(cls, message: str) -> "ModeBand":
        return cls(mode="error", source="error", gate_fail_open=False, error=message)


@dataclass(frozen=True, slots=True)
class HealthView:
    verdict: VerdictBand
    loops: LoopsBand
    capacity: CapacityBand
    mode: ModeBand


def _fail_open[Band](build: Callable[[], Band], degrade: Callable[[str], Band], label: str) -> Band:
    """Run one band's reader; degrade THAT band to a visible error on any exception.

    The observability page must never 500 because a single reader raised — one broken
    band shows its error, the other three still render (the docstring claim at module top).
    """
    try:
        return build()
    except Exception:
        logger.warning("dash health band %r read failed — degrading to an error band", label, exc_info=True)
        return degrade(f"{label} band unavailable — read failed")


def build_health_view() -> HealthView:
    """Compose all four bands. Each band fails open independently."""
    return HealthView(
        verdict=_fail_open(_verdict_band, VerdictBand.degraded, "verdict"),
        loops=_fail_open(_loops_band, LoopsBand.degraded, "loops"),
        capacity=_fail_open(_capacity_band, CapacityBand.degraded, "capacity"),
        mode=_fail_open(_mode_band, ModeBand.degraded, "mode"),
    )


def _verdict_band() -> VerdictBand:
    report = read_health()
    return VerdictBand(status=str(report.status), open_issues=report.open_issues)


def _loops_band() -> LoopsBand:
    report = build_report()
    worker_dead = not report.owner.is_live and not any(slot.held for slot in report.infra_slots)
    return LoopsBand(report=report, worker_dead=worker_dead)


def _capacity_band() -> CapacityBand:
    return CapacityBand(
        parked_lanes=_parked_lanes(),
        accounts=_account_utilizations(),
        spend=_spend_summary(),
        queue=_queue_depth(),
    )


def _parked_lanes() -> tuple[ParkedLane, ...]:
    rows = UsageWindowState.objects.active().order_by("detected_at")
    return tuple(
        ParkedLane(lane=row.lane, cause=row.cause, resets_at=row.resets_at, detected_at=row.detected_at) for row in rows
    )


def _account_utilizations() -> tuple[AccountUtilization, ...]:
    return tuple(
        AccountUtilization(
            pass_path=row.pass_path,
            utilization_5h=row.utilization_5h,
            utilization_7d=row.utilization_7d,
            is_exhausted=row.is_exhausted,
            earliest_reset=row.earliest_reset,
        )
        for row in AnthropicTokenUsage.objects.all()
    )


#: Cache key + TTL for the cycle-to-date spend aggregate. The health page polls
#: every ~5s; the aggregate is a full cycle-to-date ``TaskAttempt`` scan, so it is
#: recomputed at most once per TTL instead of once per poll (#3674 F16). A short
#: TTL keeps the chip fresh while collapsing the poll storm to one scan.
_SPEND_CACHE_KEY = "dash:health:spend_summary"
_SPEND_CACHE_TTL = 30
_SPEND_MISS = object()


def _spend_summary() -> SpendSummary | None:
    """Cached cycle-to-date spend chip — recomputed at most once per TTL, not per poll.

    Wraps :func:`_compute_spend_summary` in a short-TTL cache so the health page's
    ~5s htmx poll no longer triggers a full-table cost aggregate on every request.
    A ``None`` (fail-open) result is cached too, so a broken cost read is not
    re-hammered every poll.
    """
    cached = cache.get(_SPEND_CACHE_KEY, _SPEND_MISS)
    if cached is not _SPEND_MISS:
        return cached
    result = _compute_spend_summary()
    cache.set(_SPEND_CACHE_KEY, result, _SPEND_CACHE_TTL)
    return result


def _compute_spend_summary() -> SpendSummary | None:
    """Cycle-to-date SDK-equivalent spend vs the monthly credit (the ``t3 cost`` read).

    Composes the same ``CostReport`` the ``cost`` command builds, directly rather
    than shelling out. Fail-open to ``None`` so a broken cost read leaves the band
    without a spend chip instead of blanking the page.
    """
    try:
        settings = get_effective_settings()
        anchor = settings.billing_cycle_anchor_day or None
        today = timezone.localdate()
        start_dt = cycle_start_datetime(today, anchor_day=anchor)
        breakdown = TaskAttempt.objects.headless().filter(started_at__gte=start_dt).cost_breakdown()
        report = CostReport.build(
            breakdown,
            credit_usd=settings.sdk_monthly_credit_usd,
            cycle_start_date=cycle_start(today, anchor_day=anchor),
            today=today,
        )
    except Exception:
        logger.warning("dash spend summary read failed — omitting spend chip", exc_info=True)
        return None
    return SpendSummary(
        chip=report.chip(),
        cycle_to_date_usd=round(breakdown.total_usd, 4),
        credit_usd=report.credit_usd,
        projected_month_end_usd=round(report.projected_month_end_usd, 4),
    )


def _queue_depth() -> QueueDepth:
    return QueueDepth(headless=len(build_headless_queue()), interactive=len(build_interactive_queue()))


def _mode_band() -> ModeBand:
    resolved = resolve_active_mode()
    return ModeBand(mode=resolved.name, source=resolved.source, gate_fail_open=dash_gate_fail_open())
