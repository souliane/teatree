"""The `/dash/cycle-time/` read model — #3994's table, made continuous and drawn (#3847).

Three panels over one measurement layer (:mod:`teatree.core.cycle_time`): the aggregate
median/p90 table, a trend line per transition so a regression is visible rather than
inferred, and a stacked bar per recent ticket whose segments carry the queue/work split.
Nothing is computed here — this module positions figures the core layer produced.
"""

from dataclasses import dataclass
from datetime import datetime, timedelta

from django.db.models import Max
from django.urls import reverse
from django.utils import timezone

from teatree.core.cycle_time import (
    TicketTimeline,
    TransitionStat,
    build_ticket_timelines,
    transition_distribution,
    transition_trend,
)
from teatree.core.models.transition import TicketTransition
from teatree.core.selectors._helpers import _humanize_duration
from teatree.dash.charts import BarInput, LineSeries, StackedBar, line_series, stacked_bar
from teatree.dash.selectors import group_slug

DEFAULT_WINDOW_DAYS = 7
MAX_WINDOW_DAYS = 90
#: One bar per ticket. Past this the bars are too thin to compare, which is what the
#: chart exists for — the aggregate panel is the surface that spans everything.
TICKET_BARS = 15
#: Trend lines, slowest median first. Every edge at once is unreadable, and the
#: aggregate table below already names the ones this drops.
TREND_SERIES = 4
#: The rail hue for a span whose queue/work split cannot be measured — deliberately the
#: off-ladder grey, so an unmeasured stretch never reads as one of the four phase states.
UNMEASURED_TONE = "ignored"


@dataclass(frozen=True, slots=True)
class EdgeRow:
    """One row of the #3994 table, formatted for reading."""

    from_state: str
    to_state: str
    samples: int
    median: str
    p90: str
    median_seconds: float
    p90_seconds: float
    tone: str


@dataclass(frozen=True, slots=True)
class TicketRow:
    """A ticket's bar, with the spend that ran alongside its time."""

    number: str
    state: str
    bar: StackedBar
    lead_time: str
    queue_time: str
    work_time: str
    #: False when a phase this ticket walked has no admission stamp to measure its
    #: split with — the split cells read "—" there rather than a plausible zero.
    work_measured: bool
    cost_usd: float
    cost_estimated_usd: float

    @property
    def cost_is_wholly_estimated(self) -> bool:
        return self.cost_usd > 0 and self.cost_estimated_usd >= self.cost_usd


@dataclass(frozen=True, slots=True)
class CycleTimeView:
    window_days: int
    since: datetime
    edges: tuple[EdgeRow, ...]
    tickets: tuple[TicketRow, ...]
    trend: tuple[LineSeries, ...]
    trend_buckets: tuple[str, ...]
    trend_peak: str
    bar_scale: str


def clamp_window_days(raw: str) -> int:
    """The requested window, or the default for anything unreadable or out of range."""
    try:
        requested = int(raw)
    except (TypeError, ValueError):
        return DEFAULT_WINDOW_DAYS
    return min(max(requested, 1), MAX_WINDOW_DAYS)


def build_cycle_time_view(*, window_days: int = DEFAULT_WINDOW_DAYS) -> CycleTimeView:
    since = timezone.now() - timedelta(days=window_days)
    stats = transition_distribution(since=since)
    timelines = build_ticket_timelines(_recently_active_ticket_ids(since))
    ordered = sorted(timelines.values(), key=lambda line: line.ended_at or since, reverse=True)
    scale = max((line.lead_time_seconds for line in ordered), default=0.0)
    trend, buckets, peak = _trend(since=since, window_days=window_days, stats=stats)
    return CycleTimeView(
        window_days=window_days,
        since=since,
        edges=tuple(_edge_row(stat) for stat in stats),
        tickets=tuple(_ticket_row(line, scale=scale) for line in ordered),
        trend=trend,
        trend_buckets=buckets,
        trend_peak=_humanize_duration(peak),
        bar_scale=_humanize_duration(scale),
    )


def _recently_active_ticket_ids(since: datetime) -> list[int]:
    rows = (
        TicketTransition.objects.filter(created_at__gte=since)
        .values("ticket_id")
        .annotate(latest=Max("created_at"))
        .order_by("-latest")[:TICKET_BARS]
    )
    return [row["ticket_id"] for row in rows]


def _edge_row(stat: TransitionStat) -> EdgeRow:
    return EdgeRow(
        from_state=stat.from_state,
        to_state=stat.to_state,
        samples=stat.samples,
        median=_humanize_duration(stat.median_seconds),
        p90=_humanize_duration(stat.p90_seconds),
        median_seconds=stat.median_seconds,
        p90_seconds=stat.p90_seconds,
        tone=group_slug(stat.to_state),
    )


def _ticket_row(timeline: TicketTimeline, *, scale: float) -> TicketRow:
    pieces: list[BarInput] = []
    for segment in timeline.segments:
        tone = group_slug(segment.to_state)
        name = segment.phase or f"{segment.from_state} → {segment.to_state}"
        if segment.work_measured:
            pieces.extend(
                (
                    BarInput(label=f"{name} · waiting", tone=tone, seconds=segment.queue_seconds, muted=True),
                    BarInput(label=f"{name} · working", tone=tone, seconds=segment.work_seconds),
                )
            )
        else:
            pieces.append(BarInput(label=f"{name} · split unmeasured", tone=UNMEASURED_TONE, seconds=segment.seconds))
    return TicketRow(
        number=timeline.number,
        state=timeline.state,
        bar=stacked_bar(
            label=timeline.number,
            href=reverse("dash:ticket_drawer", args=[timeline.ticket_id]),
            pieces=pieces,
            scale_seconds=scale,
        ),
        lead_time=_humanize_duration(timeline.lead_time_seconds),
        queue_time=_humanize_duration(timeline.queue_seconds),
        work_time=_humanize_duration(timeline.work_seconds),
        work_measured=timeline.work_measured,
        cost_usd=round(timeline.cost_usd, 4),
        cost_estimated_usd=round(timeline.cost_estimated_usd, 4),
    )


def _trend(
    *,
    since: datetime,
    window_days: int,
    stats: tuple[TransitionStat, ...],
) -> tuple[tuple[LineSeries, ...], tuple[str, ...], float]:
    """The slowest few edges, bucketed — plus the shared axis and peak they scale to.

    One shared vertical scale across the series, because the question the panel answers
    is which edge dominates and whether it is growing; per-series scaling would draw a
    3-minute edge and an 85-minute one as the same line.
    """
    bucket = timedelta(days=1) if window_days > 1 else timedelta(hours=1)
    series = {(row.from_state, row.to_state): row for row in transition_trend(since=since, bucket=bucket)}
    wanted = [(stat.from_state, stat.to_state) for stat in stats[:TREND_SERIES]]
    present = [series[edge] for edge in wanted if edge in series]
    peak = max((point.median_seconds for row in present for point in row.points), default=0.0)
    buckets = sorted({point.bucket_start for row in present for point in row.points})
    labels = tuple(_bucket_label(start, bucket) for start in buckets)
    lines = tuple(
        line_series(
            label=f"{row.from_state} → {row.to_state}",
            tone=group_slug(row.to_state),
            points=[(_bucket_label(point.bucket_start, bucket), point.median_seconds) for point in row.points],
            axis=labels,
            scale_seconds=peak,
        )
        for row in present
    )
    return lines, labels, peak


def _bucket_label(start: datetime, bucket: timedelta) -> str:
    local = timezone.localtime(start)
    return local.strftime("%b %d") if bucket >= timedelta(days=1) else local.strftime("%b %d %H:%M")
