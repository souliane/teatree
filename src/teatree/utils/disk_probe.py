"""One root-filesystem percentage and critical threshold for doctor and admission."""

import os

DEFAULT_DISK_WARN_PERCENT = 85
DEFAULT_DISK_CRIT_PERCENT = 95
_PERCENT_MAX = 100


def disk_percent_threshold(raw: str | None, *, default: int) -> int:
    """Parse a 1..100 percent override; invalid values keep the shipped default."""
    if raw is None:
        return default
    try:
        value = int(raw)
    except ValueError:
        return default
    return value if 1 <= value <= _PERCENT_MAX else default


def disk_crit_percent() -> int:
    return disk_percent_threshold(os.environ.get("TEATREE_DISK_CRIT_PERCENT"), default=DEFAULT_DISK_CRIT_PERCENT)


def disk_warn_percent() -> int:
    return disk_percent_threshold(os.environ.get("TEATREE_DISK_WARN_PERCENT"), default=DEFAULT_DISK_WARN_PERCENT)


def read_disk_used_percent(path: str = "/") -> float | None:
    """Percent used on *path*'s filesystem, or unknown on a failed probe."""
    try:
        stats = os.statvfs(path)
    except OSError:
        return None
    total = stats.f_blocks * stats.f_frsize
    if total <= 0:
        return None
    return round((total - stats.f_bavail * stats.f_frsize) / total * 100)


__all__ = [
    "DEFAULT_DISK_CRIT_PERCENT",
    "DEFAULT_DISK_WARN_PERCENT",
    "disk_crit_percent",
    "disk_percent_threshold",
    "disk_warn_percent",
    "read_disk_used_percent",
]
