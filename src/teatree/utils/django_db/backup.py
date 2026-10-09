"""Control-DB backup engine — timestamped SQLite snapshots + keep-last-N-days retention (directive #2).

Directive #2 ("daily DB backup, keep last N days") is an unattended, periodic
backup of teatree's OWN control DB. This module is the single engine three
callers drive so the CLI and the loop can never diverge:

* the ``db_backup`` management command (:mod:`teatree.core.management.commands.db_backup`)
    — the manual/CLI entry point;
* the ``db_backup`` mini-loop's scanner (:mod:`teatree.loop.scanners.db_backup`)
    — cadence-gates on the newest artifact's OWN embedded timestamp, so no new
    model row / marker is needed (the same "the artifact's date is its last-run
    stamp" design the snapshot-warmer uses);
* the ``run_db_backup`` mechanical handler (:mod:`teatree.loop.mechanical_db_backup`)
    — runs the actual snapshot + prune when the scanner flags a due backup.

Env-safe (the Unit-2 pattern): the source DB is resolved from the LIVE Django
connection, mutating no process env. The snapshot reuses
:func:`teatree.paths._sqlite_snapshot` — a consistent point-in-time copy even
while the live loop holds the DB open for writing.
"""

import logging
import re
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path

from django.db import connection

from teatree.paths import _sqlite_snapshot, get_data_dir

logger = logging.getLogger(__name__)

#: The ``get_data_dir`` namespace holding the timestamped backup artifacts.
BACKUP_DIR_NAMESPACE = "backups"

#: ``strftime``/``strptime`` format for the UTC timestamp embedded in each
#: artifact's name — lexically sortable so ``max(glob)`` is the newest backup.
_TIMESTAMP_FORMAT = "%Y%m%d-%H%M%S"

#: Glob + strict parse for a backup artifact. The strict regex is the retention
#: pass's safety net: a file that is NOT a ``db-<ts>.sqlite3`` artifact (a
#: stray/foreign file dropped in the dir) never parses to a timestamp, so it is
#: never pruned — retention only ever deletes files this engine created.
_BACKUP_GLOB = "db-*.sqlite3"
_BACKUP_RE = re.compile(r"^db-(\d{8}-\d{6})\.sqlite3$")


@dataclass(frozen=True, slots=True)
class BackupResult:
    """The outcome of one :func:`run_backup` pass.

    ``created`` is the new artifact (``None`` when the pass was skipped — e.g. an
    in-memory test DB with no on-disk file to snapshot). ``pruned`` lists the
    retention-expired artifacts removed this pass. ``skipped_reason`` records why
    a pass produced no artifact so the caller can log it without guessing.
    """

    created: Path | None = None
    pruned: list[Path] = field(default_factory=list)
    skipped_reason: str | None = None


def default_backup_dir() -> Path:
    """The canonical backup dir (``<data_dir>/backups``), created if absent."""
    return get_data_dir(BACKUP_DIR_NAMESPACE)


def resolve_source_db() -> Path | None:
    """The live control DB's on-disk path, or ``None`` when it is not a real file.

    Reads the ACTIVE Django connection's ``NAME`` so a worktree-isolated DB is
    backed up as faithfully as the canonical one. Returns ``None`` for the
    in-memory test DB (``:memory:`` / ``file::memory:``) or a non-existent path —
    there is nothing on disk to snapshot.
    """
    name = connection.settings_dict.get("NAME")
    if not name:
        return None
    text = str(name)
    if ":memory:" in text or text.startswith("file::memory:"):
        return None
    path = Path(text)
    if not path.is_file():
        return None
    return path


def artifact_timestamp(name: str) -> datetime | None:
    """Parse the UTC timestamp a backup artifact's *name* embeds, or ``None``.

    ``None`` for any name that is not a ``db-<YYYYMMDD-HHMMSS>.sqlite3`` artifact,
    so a foreign file in the dir is transparent to cadence + retention.
    """
    match = _BACKUP_RE.match(name)
    if match is None:
        return None
    return datetime.strptime(match.group(1), _TIMESTAMP_FORMAT).replace(tzinfo=UTC)


def existing_backups(backup_dir: Path) -> list[Path]:
    """Every parseable backup artifact in *backup_dir*, newest last (name-sorted)."""
    if not backup_dir.is_dir():
        return []
    artifacts = [p for p in backup_dir.glob(_BACKUP_GLOB) if artifact_timestamp(p.name) is not None]
    return sorted(artifacts, key=lambda p: p.name)


def newest_backup_at(backup_dir: Path) -> datetime | None:
    """The embedded timestamp of the most recent artifact, or ``None`` when none exist."""
    artifacts = existing_backups(backup_dir)
    if not artifacts:
        return None
    return artifact_timestamp(artifacts[-1].name)


def hours_since_last_backup(backup_dir: Path, *, now: datetime) -> float | None:
    """Hours since the newest artifact's embedded timestamp, or ``None`` when none exist."""
    latest = newest_backup_at(backup_dir)
    if latest is None:
        return None
    return (now - latest).total_seconds() / 3600.0


def create_backup(*, source: Path, backup_dir: Path, now: datetime) -> Path:
    """Snapshot *source* into *backup_dir* as ``db-<now>.sqlite3`` and return the path.

    The snapshot is written to a temp sibling and atomically renamed into place so
    a concurrent reader (or a same-second re-run) never observes a partial file.
    """
    backup_dir.mkdir(parents=True, exist_ok=True)
    dest = backup_dir / f"db-{now.strftime(_TIMESTAMP_FORMAT)}.sqlite3"
    tmp = dest.with_name(f".{dest.name}.partial")
    tmp.unlink(missing_ok=True)
    try:
        _sqlite_snapshot(source, tmp)
        tmp.replace(dest)
    finally:
        tmp.unlink(missing_ok=True)
    return dest


def prune_expired(*, backup_dir: Path, retention_days: int, now: datetime) -> list[Path]:
    """Delete artifacts older than *retention_days* and return the removed paths.

    Age is read from each artifact's OWN embedded timestamp (not filesystem
    mtime), so pruning is deterministic and testable. A non-parseable file is
    never touched. ``retention_days`` is trusted to be positive — the config
    parser fails a non-positive value SAFE to the default before it reaches here.
    """
    cutoff = now - timedelta(days=retention_days)
    pruned: list[Path] = []
    for artifact in existing_backups(backup_dir):
        stamp = artifact_timestamp(artifact.name)
        if stamp is not None and stamp < cutoff:
            artifact.unlink(missing_ok=True)
            pruned.append(artifact)
    return pruned


def run_backup(
    *,
    retention_days: int,
    backup_dir: Path | None = None,
    source: Path | None = None,
    now: datetime | None = None,
) -> BackupResult:
    """Take one backup and prune retention-expired artifacts — the engine entry point.

    Resolves the backup dir and source DB from live config when not injected
    (tests pass both). A missing on-disk source (``:memory:`` test DB) skips the
    snapshot but STILL runs the prune, so retention stays enforced regardless.
    """
    resolved_dir = backup_dir if backup_dir is not None else default_backup_dir()
    resolved_now = now if now is not None else datetime.now(tz=UTC)
    resolved_source = source if source is not None else resolve_source_db()

    created: Path | None = None
    skipped_reason: str | None = None
    if resolved_source is None:
        skipped_reason = "no on-disk source DB to snapshot (in-memory or absent)"
        logger.info("db_backup: %s — pruning only", skipped_reason)
    else:
        created = create_backup(source=resolved_source, backup_dir=resolved_dir, now=resolved_now)
        logger.info("db_backup: wrote %s", created)

    pruned = prune_expired(backup_dir=resolved_dir, retention_days=retention_days, now=resolved_now)
    if pruned:
        logger.info("db_backup: pruned %d expired backup(s) past %d-day retention", len(pruned), retention_days)
    return BackupResult(created=created, pruned=pruned, skipped_reason=skipped_reason)


__all__ = [
    "BACKUP_DIR_NAMESPACE",
    "BackupResult",
    "artifact_timestamp",
    "create_backup",
    "default_backup_dir",
    "existing_backups",
    "hours_since_last_backup",
    "newest_backup_at",
    "prune_expired",
    "resolve_source_db",
    "run_backup",
]
