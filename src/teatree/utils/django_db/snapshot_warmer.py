"""Snapshot-warmer entry points (souliane/teatree#2949).

Keeps a reference DB's DSLR snapshot current out-of-band, so a
ticket-critical-path provision never has to pay the slow restore+migrate
path itself. Sibling of ``dslr`` (DSLR primitives) and ``reconcile``
(migration reconciliation) within the ``django_db`` package.
"""

from datetime import UTC, datetime
from typing import TextIO

from teatree.utils.django_db import dslr as _dslr
from teatree.utils.django_db.config import DjangoDbImportConfig
from teatree.utils.django_db.helpers import _ensure_ref_db
from teatree.utils.django_db.importer import DjangoDbImporter
from teatree.utils.django_db.migrate import _MigrateResult


def snapshot_age_days(cfg: DjangoDbImportConfig, *, now: datetime | None = None) -> int | None:
    """Age (days) of the newest DSLR snapshot for *cfg*, or ``None`` when none exists / no DSLR tool.

    A DSLR snapshot name embeds its capture date as a ``YYYYMMDD_<tenant>``
    prefix (:func:`teatree.utils.django_db.dslr.dslr_snap_name`) — the date
    the snapshot was taken doubles as its own "last refreshed" timestamp, so
    no separate persisted marker is needed.
    """
    dslr_cmd = _dslr.find_dslr_cmd(cfg.snapshot_tool, cfg.main_repo_path) if cfg.snapshot_tool else []
    if not dslr_cmd:
        return None
    env = _dslr.dslr_env(cfg.ref_db_name)
    snapshots = _dslr.find_dslr_snapshots(dslr_cmd, env, cfg.ref_db_name)
    if not snapshots:
        return None
    date_part = snapshots[0].split("_", 1)[0]
    try:
        snap_date = datetime.strptime(date_part, "%Y%m%d").replace(tzinfo=UTC)
    except ValueError:
        return None
    moment = now or datetime.now(tz=UTC)
    return max(0, (moment - snap_date).days)


def snapshot_is_stale(cfg: DjangoDbImportConfig, *, max_age_days: int = 1, now: datetime | None = None) -> bool:
    """True when *cfg*'s reference DB has no DSLR snapshot within *max_age_days*.

    No snapshot at all (or no DSLR tool configured) is always stale — there
    is nothing for the ticket-critical-path fast path to restore from.
    """
    age = snapshot_age_days(cfg, now=now)
    return age is None or age > max_age_days


def refresh_reference_snapshot(
    cfg: DjangoDbImportConfig,
    *,
    stdout: TextIO | None = None,
    stderr: TextIO | None = None,
) -> bool:
    """Bring *cfg*'s reference DB (and its DSLR snapshot) up to date with the current migration head.

    The snapshot warmer's entry point: runs out-of-band on a loop tick,
    absorbing the slow restore+migrate path so a ticket-critical-path
    provision never has to. Restores the newest available DSLR snapshot (when
    one exists), migrates, and takes a FRESH snapshot only when migrations
    were actually applied (mirrors the provisioning-time restore paths —
    :meth:`teatree.utils.django_db.importer.DjangoDbImporter._try_restore_from_dslr`).
    Returns ``True`` when the reference DB ends up migrated (whether or not a
    new snapshot was taken), ``False`` when no DSLR tool is configured or the
    restore/migrate itself failed.
    """
    importer = DjangoDbImporter(cfg, stdout=stdout, stderr=stderr)
    if not importer.dslr_cmd:
        importer.stderr.write(f"  Snapshot warmer: no DSLR tool configured for {cfg.ref_db_name}, skipping.\n")
        return False
    _ensure_ref_db(cfg.ref_db_name, importer.pg_host, importer.pg_user, importer.pg_env)
    snapshots = importer._resolve_dslr_snapshots()  # noqa: SLF001 — the warmer's own transient instance
    if snapshots:
        ok, is_env, stderr_text = _dslr.restore_ref_from_dslr(importer.dslr_cmd, importer.dslr_env, snapshots[0])
        if not ok:
            importer._log_dslr_restore_failure(snapshots[0], is_env=is_env, stderr=stderr_text)  # noqa: SLF001 — intentional access to a sibling's internal within the same subsystem
            return False
    migrate_result = importer._migrate_reference_db()  # noqa: SLF001 — intentional access to a sibling's internal within the same subsystem
    if migrate_result is _MigrateResult.FAILED:
        return False
    if migrate_result is _MigrateResult.APPLIED:
        importer._take_dslr_snapshot()  # noqa: SLF001 — intentional access to a sibling's internal within the same subsystem
    return True


__all__ = ["refresh_reference_snapshot", "snapshot_age_days", "snapshot_is_stale"]
