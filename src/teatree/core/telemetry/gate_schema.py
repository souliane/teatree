"""Reader-side validation for the stdlib hook gate ledger."""

import datetime as dt
import re
from pathlib import Path

from teatree.core.telemetry.admission_schema import _valid_trace_context
from teatree.core.telemetry.observation_read import (
    DEFAULT_WINDOW,
    READ_LIMIT_BYTES,
    ObservationRead,
    ReadPeriod,
    read_recent_observations,
)
from teatree.utils.hook_registry import loop_registry_dir

_FIELDS = frozenset({"epoch", "trace_id", "span_id", "gate", "decision", "rule"})
_SAFE_MARKER = re.compile(r"[a-z0-9_.:-]{1,64}")
_SESSION_REF = re.compile(r"[0-9a-f]{16}")


def valid_gate_row(row: object, *, cutoff: int, current: int) -> bool:
    if not isinstance(row, dict) or row.keys() - {"session_ref"} != _FIELDS or not _valid_trace_context(row):
        return False
    epoch = row["epoch"]
    if not isinstance(epoch, int) or isinstance(epoch, bool) or not cutoff <= epoch <= current:
        return False
    if any(not isinstance(row[key], str) or _SAFE_MARKER.fullmatch(row[key]) is None for key in ("gate", "rule")):
        return False
    session_ref = row.get("session_ref")
    return (
        isinstance(row["decision"], str)
        and row["decision"] in {"deny", "override", "advise"}
        and (
            "session_ref" not in row
            or (isinstance(session_ref, str) and _SESSION_REF.fullmatch(session_ref) is not None)
        )
    )


def checked_gate_observations(
    *, directory: Path | None = None, now: dt.datetime | None = None, window: dt.timedelta = DEFAULT_WINDOW
) -> ObservationRead:
    return read_recent_observations(
        directory=directory or loop_registry_dir() / "otel",
        prefix="gate",
        validator=valid_gate_row,
        period=ReadPeriod(now or dt.datetime.now(tz=dt.UTC), window),
        read_limit=READ_LIMIT_BYTES,
    )
