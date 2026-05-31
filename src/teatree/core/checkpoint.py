"""Read-then-advance checkpoint for the ``/t3:checking`` report (#1529).

The ``checking`` command answers "what did I miss since I last checked?".
That question needs a durable marker of *when the user last checked* so the
report window is ``[last_checked_at, now)``. This module owns that marker.

It is deliberately **not** a hook: a hook fires on agent lifecycle events and
would make the marker substrate. ``checking`` is a read-only user command, so
the marker advances only when the user runs the command — never as a side
effect of the loop. The command reads the prior marker, gathers the window,
then advances to ``now`` *after* gathering. Advancing last is the load-bearing
ordering: an invocation that advanced first would collapse its own window to
empty and report nothing.

The file is keyed by overlay (``T3_OVERLAY_NAME``) — per-overlay windows match
the overlay-scoped report, so checking one overlay never advances another's
marker. An empty overlay falls back to a single global file.

The marker is written via ``tmp.replace`` (atomic), mirroring
:mod:`teatree.core.availability`, so a torn write never leaves a half-encoded
JSON document; a reader tolerating a read race re-resolves cleanly to ``None``
and the window falls back to the default lookback.
"""

import fcntl
import json
import os
import tempfile
from datetime import UTC, datetime, timedelta
from pathlib import Path

from teatree.paths import DATA_DIR

#: Window size when there is no stored checkpoint and no explicit ``--since``.
#: A day matches the typical overnight-loop cadence the report serves.
DEFAULT_LOOKBACK = timedelta(hours=24)


def checkpoint_path(*, overlay: str | None = None) -> Path:
    """Location of the durable per-overlay checking-checkpoint JSON file.

    Keyed by overlay (``T3_OVERLAY_NAME`` when *overlay* is not given) so each
    overlay's last-checked marker is independent — checking one overlay must
    not advance another's window. An empty overlay falls back to a single
    global file.
    """
    name = overlay if overlay is not None else os.environ.get("T3_OVERLAY_NAME", "")
    slug = name.strip() or "global"
    return DATA_DIR / f"checking_checkpoint_{slug}.json"


def load_checkpoint(path: Path | None = None) -> datetime | None:
    """Read the stored ``last_checked_at`` timestamp, if present and well-formed.

    A missing, unreadable, malformed, or half-written file returns ``None``
    rather than raising — the caller then falls back to the default lookback,
    so a corrupt marker never blocks the report. A naive stored timestamp is
    coerced to UTC so the window comparison is always tz-aware.
    """
    target = path or checkpoint_path()
    if not target.is_file():
        return None
    try:
        raw = json.loads(target.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if not isinstance(raw, dict):
        return None
    stamp = raw.get("last_checked_at")
    if not isinstance(stamp, str) or not stamp.strip():
        return None
    try:
        parsed = datetime.fromisoformat(stamp)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed


def advance_checkpoint(now: datetime, path: Path | None = None) -> Path:
    """Atomically write ``{"last_checked_at": now}`` via ``tmp.replace``.

    *now* is stored as a tz-aware UTC ISO timestamp; a naive value is coerced
    to UTC first. The temp-file-then-rename publish means a reader never
    observes a partial document even under a concurrent invocation.
    """
    target = path or checkpoint_path()
    target.parent.mkdir(parents=True, exist_ok=True)
    now = now.replace(tzinfo=UTC) if now.tzinfo is None else now.astimezone(UTC)
    payload = {"last_checked_at": now.isoformat()}
    fd, tmp_str = tempfile.mkstemp(prefix=".checking-", suffix=".tmp", dir=str(target.parent))
    tmp_path = Path(tmp_str)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(payload, fh, sort_keys=True)
            fh.write("\n")
        tmp_path.replace(target)
    except BaseException:
        tmp_path.unlink(missing_ok=True)
        raise
    return target


def advance_checkpoint_monotonic(now: datetime, path: Path | None = None) -> Path:
    """Advance the marker to *now* only when that moves it forward in time.

    The marker records "the user has now seen everything up to *now*". Writing
    a value earlier than the stored one would re-open an already-seen window on
    the next run (double-reporting) and, more dangerously, a backward write
    after a clock regression could mark events as seen that were never
    reported. So the write is monotonic: when the stored marker is already at
    or ahead of *now* (a future/skewed marker, or a clock that went backward),
    the existing marker is kept untouched. A normal forward-moving *now* writes
    as usual. Returns the marker path either way.

    The read-compare-write is wrapped in an exclusive ``fcntl.flock`` on a
    sibling lock file so two concurrent ``checking show`` invocations are
    serialised: the loser re-reads the marker (now advanced by the winner)
    and skips the write instead of overwriting the winner's newer timestamp.
    """
    target = path or checkpoint_path()
    now_utc = now.replace(tzinfo=UTC) if now.tzinfo is None else now.astimezone(UTC)
    lock_path = target.with_suffix(".lock")
    target.parent.mkdir(parents=True, exist_ok=True)
    with lock_path.open("a", encoding="utf-8") as lock_fh:
        fcntl.flock(lock_fh, fcntl.LOCK_EX)
        try:
            stored = load_checkpoint(target)
            if stored is not None and stored >= now_utc:
                return target
            return advance_checkpoint(now_utc, target)
        finally:
            fcntl.flock(lock_fh, fcntl.LOCK_UN)


def resolve_window_start(*, since: str = "", now: datetime, path: Path | None = None) -> datetime:
    """Resolve the report window start by a single-precedence chain.

    1. **Explicit ``--since``** (ISO timestamp) — the user named the window;
        a naive value is coerced to UTC.
    2. **Stored checkpoint** — the last time the user ran ``checking``.
    3. **Default lookback** — ``now - DEFAULT_LOOKBACK`` when neither is set.

    Each layer is independently testable: pass *since* to exercise (1), seed
    the checkpoint file to exercise (2), leave both unset to exercise (3).

    **Future-start guard.** A resolved start at or after *now* would yield an
    empty ``[start, now)`` window — a future ``--since`` (typo / wrong tz) or a
    clock-skewed checkpoint written ahead of the current clock. An empty window
    silently reports nothing real, and on the default path the marker would
    then advance to *now*, permanently skipping the events between the real
    last-check and *now*. So any resolved start ``>= now`` falls back to the
    default lookback: the window is never empty, the report never silently
    skips, and the subsequent advance stays monotonic (start is always
    ``< now``).
    """
    explicit = since.strip()
    if explicit:
        parsed = datetime.fromisoformat(explicit)
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=UTC)
        return _clamp_future_start(parsed, now=now)
    stored = load_checkpoint(path)
    if stored is not None:
        return _clamp_future_start(stored, now=now)
    return now - DEFAULT_LOOKBACK


def _clamp_future_start(start: datetime, *, now: datetime) -> datetime:
    """Return *start* unless it is at/after *now*, in which case the default lookback.

    A start ``>= now`` collapses the half-open ``[start, now)`` window to
    empty; the default lookback restores a real, non-empty window.
    """
    return start if start < now else now - DEFAULT_LOOKBACK


__all__ = [
    "DEFAULT_LOOKBACK",
    "advance_checkpoint",
    "advance_checkpoint_monotonic",
    "checkpoint_path",
    "load_checkpoint",
    "resolve_window_start",
]
