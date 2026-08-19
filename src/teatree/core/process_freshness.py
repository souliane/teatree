"""Is the code THIS process loaded as new as the schema the DB has applied? — the mirror gate (#4387).

:mod:`teatree.core.schema_readiness` answers the other direction, the DB *behind* the
code, and self-heals in one tick the moment ``init`` migrates. This module answers the
one that never self-heals: a worker that imported its model classes hours before a
migration landed keeps executing them, so its INSERTs omit the new column and every
result it completes is rolled back at the record step — the run really happened, cost
real tokens, and is stored as ``failed`` (#4387, #4379, #4390). Only a restart cures it,
and nothing was measuring it.

The measurement has one trap, and it decides the whole design. Django's
``MigrationLoader.load_disk()`` calls ``reload()`` on the migrations package, so a
long-running process re-reading the graph picks up migration files that landed AFTER it
started — the reinstall drain fast-forwards the files on disk while the imported classes
stay old. A disk-re-reading predicate therefore reports CURRENT exactly when the worker
is broken: the same false green a fresh ``uv run python`` probe gives (measured, #4390).
So the comparison is between what this process FROZE at startup and what the DB has
applied NOW, and the frozen half is never re-read.

The frozen half is ``django-linear-migrations``' ``max_migration.txt`` — one small file
per app, already maintained by a repo gate, and the thing that actually carries a schema
consequence. Reading it needs no DB access (Django forbids queries in ``ready()``) and no
migration graph (which would add ~200 module imports to every ``t3`` invocation).

The fail direction is deliberately asymmetric with :mod:`~teatree.core.schema_readiness`.
Only a PROVEN skew refuses. Refusing on an ABSENT snapshot has no self-heal path at all —
the process would refuse forever, a permanent self-inflicted factory outage triggered by
an import-order quirk — whereas the schema-behind gate's fail-closed UNKNOWN clears the
moment ``init`` migrates. Every fail-open branch warns (throttled) and still publishes, so
it is visible in ``t3 doctor check`` rather than silent.

The reading is PUBLISHED because the doctor cannot measure this for itself: the watchdog
runs ``t3 doctor check`` in a FRESH process whose own snapshot is always current, so a
self-measuring check can never see the stale worker. The stale process is the only
witness there is, so it writes its verdict where a fresh reader can find it. The record is
keyed by role AND pid on purpose — a per-tick ``loops_tick`` subprocess shares the worker's
role and is always fresh, so a role-only filename would let it overwrite the stale
worker's BEHIND reading with its own CURRENT one and launder the alarm away.
"""

import enum
import json
import logging
import os
import re
import tempfile
import time
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from time import monotonic
from typing import ClassVar

from django.apps import apps
from django.db import DEFAULT_DB_ALIAS, connections
from django.db.migrations.recorder import MigrationRecorder

from teatree.core.loop_lease_liveness import reader_pid_namespace
from teatree.paths import data_dir_root
from teatree.utils.throttled_log import warn_throttled

logger = logging.getLogger(__name__)

#: How long a verdict is reused before the applied head is read again. Mirrors
#: :data:`teatree.core.schema_readiness.READINESS_TTL_SECONDS`: the claim chokepoint
#: reads this, and a per-claim query on ``django_migrations`` is a hot-path regression.
FRESHNESS_TTL_SECONDS = 60.0

#: How long a published record survives without being refreshed. A live role rewrites its
#: own record on every memo miss, so only the records of processes that have exited go
#: stale — this bounds the per-tick ``loops_tick`` subprocesses that each publish once.
RECORD_RETENTION_SECONDS = 3600.0

RECORD_PREFIX = "process-freshness-"
RECORD_SUFFIX = ".json"

_MAX_MIGRATION_FILENAME = "max_migration.txt"
_UNATTRIBUTED_ROLE = "unattributed"
_FILENAME_UNSAFE = re.compile(r"[^A-Za-z0-9._-]+")
_NUMERIC_PREFIX = re.compile(r"^(\d+)_")

_MEMO: dict[str, tuple[float, "FreshnessReading"]] = {}


class FreshnessVerdict(enum.StrEnum):
    """Where this process's loaded code sits relative to the applied schema."""

    CURRENT = "current"
    BEHIND = "behind"
    UNKNOWN = "unknown"


@dataclass(frozen=True, slots=True)
class LoadedSnapshot:
    """What this interpreter froze at startup — the half of the comparison disk cannot move."""

    heads: Mapping[str, str]
    started_at: datetime


@dataclass(frozen=True, slots=True)
class FreshnessReading:
    """One comparison of the frozen snapshot against the schema applied right now."""

    verdict: FreshnessVerdict
    app_label: str = ""
    loaded_head: str = ""
    applied_head: str = ""
    applied_at: str = ""
    detail: str = ""

    def block_reason(self) -> str:
        """The operator-facing refusal, or ``""`` when work may be admitted."""
        if self.verdict is not FreshnessVerdict.BEHIND:
            return ""
        return (
            f"this process loaded {self.app_label} at migration {self.loaded_head} but the control "
            f"DB has applied {self.applied_head} (at {self.applied_at}) — the model classes in "
            f"memory predate the schema, so a result recorded here would roll back and a finished "
            f"run would be stored as failed. Restart this role's container when no task is claimed."
        )


@dataclass(frozen=True, slots=True)
class PublishedReading:
    """A record a (possibly stale) process wrote for a fresh reader to find."""

    role: str
    pid: int
    verdict: str
    loaded_head: str
    applied_head: str
    applied_at: str
    process_started_at: str
    at: str
    detail: str = ""

    @classmethod
    def from_payload(cls, payload: Mapping[str, object]) -> "PublishedReading":
        return cls(
            role=str(payload.get("role", "")),
            pid=_as_pid(payload.get("pid")),
            verdict=str(payload.get("verdict", "")),
            loaded_head=str(payload.get("loaded_head", "")),
            applied_head=str(payload.get("applied_head", "")),
            applied_at=str(payload.get("applied_at", "")),
            process_started_at=str(payload.get("process_started_at", "")),
            at=str(payload.get("at", "")),
            detail=str(payload.get("detail", "")),
        )

    @property
    def is_behind(self) -> bool:
        return self.verdict == FreshnessVerdict.BEHIND

    @property
    def is_unknown(self) -> bool:
        return self.verdict == FreshnessVerdict.UNKNOWN

    def age_seconds(self, now: datetime) -> float | None:
        try:
            return (now - datetime.fromisoformat(self.at)).total_seconds()
        except (TypeError, ValueError):
            # Unparsable, or naive against an aware ``now``. ``None`` is the honest answer
            # and the doctor treats it as "too old to trust" rather than tracebacking.
            return None

    def describe(self) -> str:
        return (
            f"role={self.role} pid={self.pid} loaded={self.loaded_head} applied={self.applied_head} "
            f"applied_at={self.applied_at} process_started_at={self.process_started_at}"
        )


def _as_pid(raw: object) -> int:
    try:
        return int(str(raw))
    except ValueError:
        return 0


class _ProcessSnapshot:
    """Module state for the frozen snapshot, on a class so rebinding needs no ``global``."""

    frozen: ClassVar[LoadedSnapshot | None] = None


def _disk_migration_heads() -> dict[str, str]:
    heads: dict[str, str] = {}
    for config in apps.get_app_configs():
        try:
            declared = (Path(config.path) / "migrations" / _MAX_MIGRATION_FILENAME).read_text(encoding="utf-8")
        except OSError:
            continue
        lines = [line.strip() for line in declared.splitlines() if line.strip()]
        if lines:
            heads[config.label] = lines[0]
    return heads


def record_loaded_snapshot() -> None:
    """Freeze what this process loaded — called once from ``CoreConfig.ready()``, zero DB access.

    Idempotent by design: a second call (Django re-runs ``ready()`` under
    ``override_settings(INSTALLED_APPS=...)``) must NOT re-read disk, because a
    re-read is precisely the false-green this module exists to prevent.
    """
    if _ProcessSnapshot.frozen is None:
        _ProcessSnapshot.frozen = LoadedSnapshot(heads=_disk_migration_heads(), started_at=datetime.now(UTC))


def reset_loaded_snapshot() -> None:
    """Drop the frozen snapshot — test-only, so a case can freeze one it controls."""
    _ProcessSnapshot.frozen = None


def _sequence(migration_name: str) -> int | None:
    match = _NUMERIC_PREFIX.match(migration_name)
    return int(match.group(1)) if match else None


def _compare(snapshot: LoadedSnapshot, alias: str) -> FreshnessReading:
    if not snapshot.heads:
        # The frozen half is missing entirely, so nothing was measured. Falling through to the
        # CURRENT seed below would publish that as the healthiest possible answer.
        warn_throttled(
            logger,
            "process-freshness:no-heads",
            "process-freshness froze no max_migration.txt from any app — reporting unknown, not current",
        )
        return FreshnessReading(
            verdict=FreshnessVerdict.UNKNOWN,
            detail="no app declared a readable max_migration.txt, so no head was frozen to compare",
        )
    recorder = MigrationRecorder(connections[alias])
    current = FreshnessReading(verdict=FreshnessVerdict.CURRENT)
    highest_applied = -1
    for app_label, loaded_head in sorted(snapshot.heads.items()):
        row = recorder.migration_qs.filter(app=app_label).order_by("-id").first()
        if row is None:
            continue
        loaded_sequence, applied_sequence = _sequence(loaded_head), _sequence(row.name)
        applied_at = row.applied.isoformat() if row.applied else ""
        if loaded_sequence is None or applied_sequence is None:
            # A squash (``0001_squashed_0030``) or a hand-named migration: a prefix this
            # cannot order is not evidence of skew, so it admits rather than guessing.
            warn_throttled(
                logger,
                f"process-freshness:unparsable:{app_label}",
                "process-freshness cannot order %s loaded=%s applied=%s — admitting",
                app_label,
                loaded_head,
                row.name,
            )
            continue
        if applied_sequence > loaded_sequence:
            return FreshnessReading(
                verdict=FreshnessVerdict.BEHIND,
                app_label=app_label,
                loaded_head=loaded_head,
                applied_head=row.name,
                applied_at=applied_at,
            )
        if applied_sequence > highest_applied:
            highest_applied = applied_sequence
            current = FreshnessReading(
                verdict=FreshnessVerdict.CURRENT,
                app_label=app_label,
                loaded_head=loaded_head,
                applied_head=row.name,
                applied_at=applied_at,
            )
    return current


def read_process_freshness(alias: str = DEFAULT_DB_ALIAS) -> FreshnessReading:
    """Compare the frozen snapshot against the applied schema now — never memoised.

    ``UNKNOWN`` is the honest answer both when no snapshot was ever frozen and when the
    ``django_migrations`` read raises; neither is laundered into ``CURRENT``, and neither
    refuses admission (see the module docstring on why fail-open is right *here*).
    """
    snapshot = _ProcessSnapshot.frozen
    if snapshot is None:
        return FreshnessReading(verdict=FreshnessVerdict.UNKNOWN, detail="no startup snapshot was frozen")
    try:
        return _compare(snapshot, alias)
    except Exception as exc:  # noqa: BLE001 — a failed probe is UNKNOWN, never a silent CURRENT
        return FreshnessReading(verdict=FreshnessVerdict.UNKNOWN, detail=f"{exc.__class__.__name__}: {exc}")


def cached_process_freshness(alias: str = DEFAULT_DB_ALIAS) -> FreshnessReading:
    """The memoised verdict for *alias*, re-reading (and re-publishing) once its TTL elapses."""
    now = monotonic()
    entry = _MEMO.get(alias)
    if entry is not None and now < entry[0]:
        return entry[1]
    reading = read_process_freshness(alias)
    _MEMO[alias] = (now + FRESHNESS_TTL_SECONDS, reading)
    publish_freshness_reading(reading)
    return reading


def invalidate_process_freshness(alias: str | None = None) -> None:
    """Drop the memoised verdict so the next call re-reads the applied head."""
    if alias is None:
        _MEMO.clear()
    else:
        _MEMO.pop(alias, None)


def code_behind_schema(alias: str = DEFAULT_DB_ALIAS) -> str:
    """Why this process must not claim work right now, or ``""`` to admit.

    The claim chokepoint's face of :func:`cached_process_freshness`.
    """
    reading = cached_process_freshness(alias)
    if reading.verdict is FreshnessVerdict.UNKNOWN:
        warn_throttled(
            logger,
            "process-freshness:unknown",
            "process-freshness unreadable (%s) — admitting work rather than stalling this role forever",
            reading.detail,
        )
    return reading.block_reason()


def _record_path(role: str, pid: int) -> Path:
    safe_role = _FILENAME_UNSAFE.sub("-", role.strip()) or _UNATTRIBUTED_ROLE
    return data_dir_root() / f"{RECORD_PREFIX}{safe_role}-{pid}{RECORD_SUFFIX}"


def _prune_expired_records(directory: Path, *, keep: Path) -> None:
    cutoff = time.time() - RECORD_RETENTION_SECONDS
    for record in directory.glob(f"{RECORD_PREFIX}*{RECORD_SUFFIX}"):
        if record == keep:
            continue
        try:
            if record.stat().st_mtime < cutoff:
                record.unlink()
        except OSError:
            continue


def publish_freshness_reading(reading: FreshnessReading) -> None:
    """Write *reading* where a FRESH reader (``t3 doctor check``) can find it.

    Atomic (write-then-``replace``) so a reader never sees a half-written record, and
    best-effort: a data dir this process cannot write is a throttled warning, never a
    reason to stop the claim path it rides on.
    """
    snapshot = _ProcessSnapshot.frozen
    pid = os.getpid()
    payload = {
        "role": os.environ.get("TEATREE_ROLE", "").strip(),
        "pid": pid,
        "pid_namespace": reader_pid_namespace(),
        "process_started_at": snapshot.started_at.isoformat() if snapshot else "",
        "app_label": reading.app_label,
        "loaded_head": reading.loaded_head,
        "applied_head": reading.applied_head,
        "applied_at": reading.applied_at,
        "verdict": str(reading.verdict),
        "detail": reading.detail,
        "at": datetime.now(UTC).isoformat(),
    }
    target = _record_path(str(payload["role"]), pid)
    try:
        target.parent.mkdir(parents=True, exist_ok=True)
        handle, staged = tempfile.mkstemp(prefix=f".{RECORD_PREFIX}", suffix=".tmp", dir=target.parent)
        with os.fdopen(handle, "w", encoding="utf-8") as stream:
            json.dump(payload, stream, indent=2)
        Path(staged).replace(target)
        _prune_expired_records(target.parent, keep=target)
    except OSError as exc:
        warn_throttled(
            logger,
            "process-freshness:publish",
            "process-freshness record not published to %s: %s",
            target,
            exc,
        )


def published_readings() -> list[PublishedReading]:
    """Every record on disk, newest first — what the doctor reads INSTEAD of measuring itself."""
    readings: list[PublishedReading] = []
    try:
        records = sorted(data_dir_root().glob(f"{RECORD_PREFIX}*{RECORD_SUFFIX}"))
    except OSError:
        return []
    for record in records:
        try:
            payload = json.loads(record.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        if isinstance(payload, dict):
            readings.append(PublishedReading.from_payload(payload))
    return sorted(readings, key=lambda reading: reading.at, reverse=True)


__all__ = [
    "FRESHNESS_TTL_SECONDS",
    "RECORD_PREFIX",
    "RECORD_RETENTION_SECONDS",
    "RECORD_SUFFIX",
    "FreshnessReading",
    "FreshnessVerdict",
    "LoadedSnapshot",
    "PublishedReading",
    "cached_process_freshness",
    "code_behind_schema",
    "invalidate_process_freshness",
    "publish_freshness_reading",
    "published_readings",
    "read_process_freshness",
    "record_loaded_snapshot",
    "reset_loaded_snapshot",
]
