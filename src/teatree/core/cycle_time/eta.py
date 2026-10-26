"""Measured remaining-time arithmetic for the checking report."""

from collections.abc import Sequence
from math import ceil

from teatree.core.cycle_time.distribution import percentile


def remaining_given_elapsed(samples: Sequence[float], elapsed_minutes: float) -> tuple[float, float] | None:
    survivors = [value - elapsed_minutes for value in samples if value > elapsed_minutes]
    if not survivors:
        return None
    return percentile(survivors, 0.5), percentile(survivors, 0.8)


def queue_waves(*, ahead: int, occupied: int, seats: int) -> int | None:
    if seats <= 0:
        return None
    return ceil(max(0, ahead + 1 + occupied - seats) / seats)


def format_minutes(minutes: float) -> str:
    rounded = round(minutes)
    hours, remainder = divmod(rounded, 60)
    return f"{hours}h{remainder:02d}m" if hours else f"{remainder}m"
