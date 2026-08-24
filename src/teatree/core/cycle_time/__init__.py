"""Cycle-time measurement (#3847) — per-phase duration, lead time, and the aggregate.

The public surface every reader (dashboard, tests, future CLI) uses. Durations come
from :class:`~teatree.core.models.transition.TicketTransition` deltas and from
``Task.admitted_at``; ``TaskAttempt.started_at`` is never a start time — it is stamped
at insert, and the insert happens at agent completion.
"""

from teatree.core.cycle_time.distribution import (
    TransitionStat,
    TransitionTrend,
    TrendPoint,
    percentile,
    transition_distribution,
    transition_trend,
)
from teatree.core.cycle_time.spans import PhaseSpan, spans_for_tickets, spans_since
from teatree.core.cycle_time.timeline import PhaseSegment, TicketTimeline, build_ticket_timeline, build_ticket_timelines

__all__ = [
    "PhaseSegment",
    "PhaseSpan",
    "TicketTimeline",
    "TransitionStat",
    "TransitionTrend",
    "TrendPoint",
    "build_ticket_timeline",
    "build_ticket_timelines",
    "percentile",
    "spans_for_tickets",
    "spans_since",
    "transition_distribution",
    "transition_trend",
]
