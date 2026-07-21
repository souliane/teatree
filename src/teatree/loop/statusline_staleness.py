"""Render-age freshness gate for the statusline (the months-long stale-info bug).

Both statusline readers — the shell hook ``hooks/scripts/statusline.sh`` and
the ``t3 loop status`` CLI — display the pre-rendered statusline file
verbatim. The render is decoupled from the read so the hook stays fast
(<10ms), but that decoupling has a cost: when the loop stops ticking (a
dead loop, a stopped cron, a long pause), the file is frozen and every
reader shows a confident, hours-old loop line — "next tick 4m" that will
never come — with no signal that the information is stale.

This module is the single home for the freshness decision:

* the cutoff arithmetic (``max(2 * cadence, FLOOR_SECONDS)``), and
* the wording of the RED stale banner the readers prepend.

The shell hook mirrors the same arithmetic inline (it cannot import
Python and stay fast/dependency-free); ``tests/test_claude_statusline.py``
pins the two implementations to the same boundary so they cannot drift.

The render age is read from the ``rendered_at`` epoch in the
``tick-meta.json`` sidecar (written by :mod:`teatree.loop.tick_freshness`).
Both the Python reader here and the shell hook **fail open** — a missing
sidecar, an unreadable file, an absent ``rendered_at`` key, or a broken
cadence all resolve to "not stale" so a freshness probe can never blank
or corrupt the statusline.
"""

import json
import time
from pathlib import Path

#: A render older than ``max(STALE_CADENCE_MULTIPLIER * cadence, FLOOR_SECONDS)``
#: is treated as frozen. The 2x multiplier mirrors the established dead-loop
#: TTL in :mod:`teatree.loop.admit_budget`; the floor keeps a very short
#: cadence (e.g. a 60s test loop) from flagging stale on a single skipped tick.
STALE_CADENCE_MULTIPLIER = 2
FLOOR_SECONDS = 300

_SECONDS_PER_MINUTE = 60
_SECONDS_PER_HOUR = 3600
_SECONDS_PER_DAY = 86400

#: ANSI bold-red, matching the action_needed zone color the readers use.
_RED = "\033[1;31m"
_RST = "\033[0m"


def stale_cutoff_seconds(cadence_seconds: int) -> int:
    """The render-age beyond which the statusline is considered frozen."""
    return max(STALE_CADENCE_MULTIPLIER * int(cadence_seconds), FLOOR_SECONDS)


def _format_age(age_seconds: int) -> str:
    """Compact human age — ``45s`` / ``12m`` / ``6h`` / ``3d``."""
    if age_seconds < _SECONDS_PER_MINUTE:
        return f"{age_seconds}s"
    if age_seconds < _SECONDS_PER_HOUR:
        return f"{age_seconds // _SECONDS_PER_MINUTE}m"
    if age_seconds < _SECONDS_PER_DAY:
        return f"{age_seconds // _SECONDS_PER_HOUR}h"
    return f"{age_seconds // _SECONDS_PER_DAY}d"


def staleness_banner(age_seconds: int, *, colorize: bool = True) -> str:
    """The one-line RED banner shown above a frozen statusline.

    The single home for the wording so both readers (and the shell hook,
    which mirrors this text) read identically.
    """
    age = _format_age(int(age_seconds))
    text = (
        f"⚠ statusline STALE — last rendered {age} ago; loop may be stopped "
        "(re-register its /loop via /t3:loops, or run `t3 loops tick`)"
    )
    if colorize:
        return f"{_RED}{text}{_RST}"
    return text


def _meta_path(statusline_path: Path) -> Path:
    return statusline_path.with_name("tick-meta.json")


def _rendered_at(meta_path: Path) -> float | None:
    """Read ``rendered_at`` from the sidecar; ``None`` when unavailable.

    Fails open (``None`` → "not stale") on a missing/unreadable file, a
    non-object body, an absent or non-numeric ``rendered_at``.
    """
    try:
        data = json.loads(meta_path.read_text(encoding="utf-8"))
    except (FileNotFoundError, OSError, json.JSONDecodeError):
        return None
    if not isinstance(data, dict):
        return None
    value = data.get("rendered_at")
    if isinstance(value, (int, float)):
        return float(value)
    return None


def render_age_seconds(statusline_path: Path, *, now: float | None = None) -> float | None:
    """The age in seconds of *statusline_path*'s last render, or ``None`` when unknown.

    Reads the ``rendered_at`` epoch from the ``tick-meta.json`` sidecar next to
    *statusline_path* — the single source both the stale-banner readers and the
    headless render-refresh chain share, so a "how old is the statusline" decision
    can never drift between the writer that keeps it fresh and the probes that flag
    it stale. Fails open to ``None`` (age unknown) on a missing/unreadable sidecar
    or an absent ``rendered_at``, exactly like :func:`staleness_banner_for`.
    """
    rendered_at = _rendered_at(_meta_path(statusline_path))
    if rendered_at is None:
        return None
    return (time.time() if now is None else now) - rendered_at


def staleness_banner_for(
    statusline_path: Path,
    *,
    cadence_seconds: int,
    now: float | None = None,
    colorize: bool = True,
) -> str:
    """Return the stale banner for *statusline_path*, or ``""`` when fresh.

    Resolves the render age from the ``tick-meta.json`` sidecar next to
    *statusline_path* and compares it to :func:`stale_cutoff_seconds`.
    Fails open to ``""`` (no banner) whenever the render time cannot be
    determined — a freshness probe must never suppress real content.
    """
    age = render_age_seconds(statusline_path, now=now)
    if age is None or age <= stale_cutoff_seconds(cadence_seconds):
        return ""
    return staleness_banner(int(age), colorize=colorize)
