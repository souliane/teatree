"""Union of the stretches an agent held a phase, so parallel work is not double-counted.

Two tasks dispatched into the same phase overlap in wall-clock time. Summing their
lengths reports more work than the phase lasted (and makes queue wait go negative),
so the stretches are merged before they are measured.
"""

from collections.abc import Iterable
from dataclasses import dataclass
from datetime import datetime


@dataclass(frozen=True, slots=True)
class Engagement:
    """One task's stretch of agent engagement — ``end`` of ``None`` means still open."""

    start: datetime
    end: datetime | None


def merged_seconds(engagements: Iterable[Engagement], *, start: datetime, end: datetime) -> float:
    """Elapsed seconds inside ``[start, end]`` covered by at least one engagement."""
    merged: list[tuple[datetime, datetime]] = []
    for lower, upper in sorted(_clip(engagements, start=start, end=end)):
        if merged and lower <= merged[-1][1]:
            open_from, open_to = merged[-1]
            merged[-1] = (open_from, max(open_to, upper))
        else:
            merged.append((lower, upper))
    return sum((upper - lower).total_seconds() for lower, upper in merged)


def _clip(
    engagements: Iterable[Engagement],
    *,
    start: datetime,
    end: datetime,
) -> Iterable[tuple[datetime, datetime]]:
    for engagement in engagements:
        lower = max(engagement.start, start)
        upper = min(engagement.end or end, end)
        if upper > lower:
            yield lower, upper
