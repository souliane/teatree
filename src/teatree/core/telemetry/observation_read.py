"""Bounded local observation reads with an explicit completeness contract."""

import datetime as dt
import json
import logging
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

from teatree.utils.throttled_log import warn_throttled

RETENTION_DAYS = 7
DEFAULT_WINDOW = dt.timedelta(minutes=30)
READ_LIMIT_BYTES = 32 * 1024 * 1024
logger = logging.getLogger(__name__)
_DAMAGED = frozenset({"otel_malformed", "otel_truncated"})
_DAMAGE_WARNING_SECONDS = RETENTION_DAYS * 24 * 60 * 60


class RowValidator(Protocol):
    def __call__(self, row: object, *, cutoff: int, current: int) -> bool: ...


@dataclass(frozen=True, slots=True)
class ObservationRead:
    rows: list[dict]
    complete: bool
    reason: str | None = None


@dataclass(frozen=True, slots=True)
class ReadPeriod:
    now: dt.datetime
    window: dt.timedelta


@dataclass(frozen=True, slots=True)
class _TailRead:
    lines: list[bytes]
    complete: bool
    reason: str | None = None


def _tail(path: Path, limit: int) -> _TailRead:
    try:
        with path.open("rb") as source:
            source.seek(0, os.SEEK_END)
            size = source.tell()
            source.seek(max(0, size - limit))
            data = source.read(limit)
    except FileNotFoundError:
        return _TailRead([], complete=False, reason="otel_missing")
    except OSError:
        return _TailRead([], complete=False, reason="otel_unreadable")

    lines = data.splitlines()
    if size > limit:
        return _TailRead(lines[1:], complete=False, reason="otel_bounded")
    if len(data) != size or (data and not data.endswith(b"\n")):
        return _TailRead(lines, complete=False, reason="otel_truncated")
    return _TailRead(lines, complete=True)


def _prefer_nonmissing_reason(current: str | None, incoming: str | None) -> str | None:
    """A sparse day must never hide a damaged day in a multi-day read."""
    if incoming is None:
        return current
    return incoming if current is None or current == "otel_missing" else current


def read_recent_observations(
    *,
    directory: Path,
    prefix: str,
    validator: RowValidator,
    period: ReadPeriod,
    read_limit: int,
) -> ObservationRead:
    now = period.now
    window = period.window
    if window <= dt.timedelta(0):
        return ObservationRead([], complete=False, reason="otel_invalid_window")
    if window > dt.timedelta(days=RETENTION_DAYS):
        return ObservationRead([], complete=False, reason="otel_window_exceeds_retention")
    cutoff = int((now - window).timestamp())
    current = int(now.timestamp())
    first_day = (now - window).date()
    day_count = (now.date() - first_day).days + 1
    per_day_limit = min(read_limit // 2, read_limit // day_count)
    if per_day_limit <= 0:
        return ObservationRead([], complete=False, reason="otel_bounded")
    days = (first_day + dt.timedelta(days=offset) for offset in range(day_count))
    rows: list[dict] = []
    reason: str | None = None
    for day in days:
        tail = _tail(directory / f"{prefix}-{day.isoformat()}.jsonl", per_day_limit)
        day_reason = tail.reason
        reason = _prefer_nonmissing_reason(reason, tail.reason)
        for line in tail.lines:
            try:
                row = json.loads(line)
            except (ValueError, UnicodeDecodeError, RecursionError):
                day_reason = "otel_malformed"
                reason = _prefer_nonmissing_reason(reason, "otel_malformed")
                continue
            if isinstance(row, dict) and isinstance(row.get("epoch"), int) and row["epoch"] < cutoff:
                continue
            if validator(row, cutoff=cutoff, current=current):
                rows.append(row)
            else:
                day_reason = "otel_malformed"
                reason = _prefer_nonmissing_reason(reason, "otel_malformed")
        if day_reason in _DAMAGED:
            warn_throttled(
                logger,
                f"otel-damaged:{prefix}:{day}:{day_reason}",
                "OTel %s %s: damaged lines skipped, valid rows kept (%s)",
                prefix,
                day,
                day_reason,
                window_seconds=_DAMAGE_WARNING_SECONDS,
            )
    return ObservationRead(rows, reason is None, reason)
