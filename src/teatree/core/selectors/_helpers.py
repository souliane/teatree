import os
from pathlib import Path

from django.utils import timezone

from teatree.core.models import Ticket

_CLAUDE_SESSIONS_DIR = Path(os.environ.get("TEATREE_CLAUDE_SESSIONS_DIR") or Path.home() / ".claude" / "sessions")

_SECONDS_PER_MINUTE = 60
_MINUTES_PER_HOUR = 60
_MS_PER_MINUTE = 60_000


def _display_id(ticket: Ticket) -> str:
    return ticket.ticket_number


def _extra_str(ticket: Ticket, key: str) -> str:
    extra = ticket.extra if isinstance(ticket.extra, dict) else {}
    return str(extra.get(key, ""))


def _humanize_duration(seconds: float) -> str:
    """Format seconds into a short human-readable string like '2m 30s' or '1h 15m'."""
    total = max(0, int(seconds))
    if total < _SECONDS_PER_MINUTE:
        return f"{total}s"
    minutes, secs = divmod(total, _SECONDS_PER_MINUTE)
    if minutes < _MINUTES_PER_HOUR:
        return f"{minutes}m {secs}s" if secs else f"{minutes}m"
    hours, mins = divmod(minutes, _MINUTES_PER_HOUR)
    return f"{hours}h {mins}m" if mins else f"{hours}h"


def _list_of_str(value: object) -> list[str]:
    if isinstance(value, list):
        return [str(v) for v in value]
    return []


def _uptime_from_epoch_ms(started_at_ms: int) -> str:
    """Convert epoch milliseconds to a human-readable uptime string."""
    elapsed = int(timezone.now().timestamp() * 1000) - started_at_ms
    minutes = max(0, elapsed // _MS_PER_MINUTE)
    if minutes < _MINUTES_PER_HOUR:
        return f"{minutes}m"
    hours = minutes // _MINUTES_PER_HOUR
    remaining = minutes % _MINUTES_PER_HOUR
    return f"{hours}h{remaining:02d}m"
