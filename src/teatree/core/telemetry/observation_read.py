"""Bounded local observation reads with an explicit completeness contract."""

import datetime as dt
import json
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol


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


def read_recent_observations(
    *,
    directory: Path,
    prefix: str,
    validator: RowValidator,
    period: ReadPeriod,
    tail_limit: int,
) -> ObservationRead:
    now = period.now
    window = period.window
    cutoff = int((now - window).timestamp())
    current = int(now.timestamp())
    days = ((now - window).date(), now.date())
    rows: list[dict] = []
    reason: str | None = None
    for day in dict.fromkeys(days):
        tail = _tail(directory / f"{prefix}-{day.isoformat()}.jsonl", tail_limit)
        reason = reason or tail.reason
        for line in tail.lines:
            try:
                row = json.loads(line)
            except (ValueError, UnicodeDecodeError, RecursionError):
                reason = reason or "otel_malformed"
                continue
            if isinstance(row, dict) and isinstance(row.get("epoch"), int) and row["epoch"] < cutoff:
                continue
            if validator(row, cutoff=cutoff, current=current):
                rows.append(row)
            else:
                reason = reason or "otel_malformed"
    return ObservationRead(rows, reason is None, reason)
