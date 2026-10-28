"""Formatting helpers safe to import before Django app setup."""

from datetime import timedelta

_MINUTES_PER_HOUR = 60
_HOURS_PER_DAY = 24


def format_age(age: timedelta) -> str:
    """Render *age* as a compact ``2d`` / ``5h`` / ``7m`` / ``now``."""
    seconds = max(int(age.total_seconds()), 0)
    minutes = seconds // 60
    if minutes < 1:
        return "now"
    hours = minutes // _MINUTES_PER_HOUR
    if hours < 1:
        return f"{minutes}m"
    days = hours // _HOURS_PER_DAY
    if days < 1:
        return f"{hours}h"
    return f"{days}d"
