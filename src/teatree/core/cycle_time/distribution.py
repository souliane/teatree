"""Median AND p90 per transition over a window — #3994's table, computed continuously.

#3994 produced this by hand once and named `planned -> coded` and `reviewed -> shipped`
as 71% of a 210-minute median. The median alone is not enough to keep it: a factory
whose median holds while its p90 doubles is a factory that lost a delivery day, so
every edge carries both.
"""

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta

from teatree.core.cycle_time.spans import PhaseSpan, spans_since

MEDIAN = 0.5
P90 = 0.9


@dataclass(frozen=True, slots=True)
class TransitionStat:
    from_state: str
    to_state: str
    samples: int
    median_seconds: float
    p90_seconds: float


@dataclass(frozen=True, slots=True)
class TrendPoint:
    bucket_start: datetime
    samples: int
    median_seconds: float


@dataclass(frozen=True, slots=True)
class TransitionTrend:
    from_state: str
    to_state: str
    points: tuple[TrendPoint, ...]


def transition_distribution(
    *,
    since: datetime,
    until: datetime | None = None,
    overlay: str = "",
) -> tuple[TransitionStat, ...]:
    """One row per transition edge, slowest median first — the whale leads the table."""
    by_edge = _by_edge(spans_since(since, until, overlay=overlay))
    stats = [
        TransitionStat(
            from_state=from_state,
            to_state=to_state,
            samples=len(durations),
            median_seconds=percentile(durations, MEDIAN),
            p90_seconds=percentile(durations, P90),
        )
        for (from_state, to_state), durations in by_edge.items()
    ]
    return tuple(sorted(stats, key=lambda stat: (-stat.median_seconds, stat.from_state, stat.to_state)))


def transition_trend(
    *,
    since: datetime,
    bucket: timedelta,
    until: datetime | None = None,
    overlay: str = "",
) -> tuple[TransitionTrend, ...]:
    """Each edge's median per time bucket — the "is the factory getting slower" read.

    Buckets are anchored on *since* rather than on the calendar, so the leftmost bucket
    is always a full one and a window's first point is never a fragment that reads as a
    dip. An empty bucket yields no point rather than a zero.
    """
    spans = spans_since(since, until, overlay=overlay)
    buckets: dict[tuple[str, str], dict[datetime, list[float]]] = {}
    for span in spans:
        index = int((span.left_at - since) / bucket)
        edge = buckets.setdefault((span.from_state, span.to_state), {})
        edge.setdefault(since + index * bucket, []).append(span.seconds)
    trends = [
        TransitionTrend(
            from_state=from_state,
            to_state=to_state,
            points=tuple(
                TrendPoint(bucket_start=start, samples=len(values), median_seconds=percentile(values, MEDIAN))
                for start, values in sorted(per_bucket.items())
            ),
        )
        for (from_state, to_state), per_bucket in buckets.items()
    ]
    return tuple(sorted(trends, key=lambda trend: (trend.from_state, trend.to_state)))


def percentile(values: Sequence[float], fraction: float) -> float:
    """Linearly interpolated percentile — defined for a single sample, unlike ``quantiles``.

    ``statistics.quantiles`` raises below two data points, and an edge the factory has
    walked once is exactly the edge worth seeing.
    """
    ordered = sorted(values)
    if not ordered:
        return 0.0
    position = fraction * (len(ordered) - 1)
    lower = int(position)
    upper = min(lower + 1, len(ordered) - 1)
    return ordered[lower] + (ordered[upper] - ordered[lower]) * (position - lower)


def _by_edge(spans: Sequence[PhaseSpan]) -> dict[tuple[str, str], list[float]]:
    grouped: dict[tuple[str, str], list[float]] = {}
    for span in spans:
        grouped.setdefault((span.from_state, span.to_state), []).append(span.seconds)
    return grouped
