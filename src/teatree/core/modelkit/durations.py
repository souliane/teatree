"""The one duration-spec parser behind every ``--for``/``--since`` flag."""

import datetime as dt
import re

_DURATION_RE = re.compile(r"^(\d+)([smhd])$")
_UNIT_SECONDS = {"s": 1, "m": 60, "h": 3600, "d": 86400}


def parse_duration(raw: str, *, flag: str) -> dt.timedelta:
    """Resolve a ``2h``/``30m``/``1d`` spec, naming *flag* in the refusal so the operator knows which one was wrong."""
    match = _DURATION_RE.match(raw.strip())
    if match is None:
        msg = f"invalid {flag} duration {raw!r}; use forms like 2h, 30m, 1d"
        raise ValueError(msg)
    return dt.timedelta(seconds=int(match.group(1)) * _UNIT_SECONDS[match.group(2)])


def format_window(window: dt.timedelta) -> str:
    """Render *window* in the largest unit that divides it evenly (``86400s`` → ``1d``)."""
    seconds = int(window.total_seconds())
    for unit, size in (("d", 86400), ("h", 3600), ("m", 60)):
        if seconds >= size and seconds % size == 0:
            return f"{seconds // size}{unit}"
    return f"{seconds}s"


def format_age(stamp: dt.datetime | None, *, now: dt.datetime) -> str:
    """How long ago *stamp* was, in one coarse unit — the operator needs the magnitude, not the precision."""
    if stamp is None:
        return "undated"
    seconds = max(int((now - stamp).total_seconds()), 0)
    for unit, size in (("d", 86400), ("h", 3600), ("m", 60)):
        if seconds >= size:
            return f"{seconds // size}{unit} ago"
    return f"{seconds}s ago"
