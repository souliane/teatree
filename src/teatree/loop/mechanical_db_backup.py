"""DB-backup mechanical handler — the executor for ``db_backup.due`` (directive #2).

The scanner (:mod:`teatree.loop.scanners.db_backup`) only FLAGS that a backup is
due; this module runs the actual snapshot + retention prune via the shared engine
(:mod:`teatree.utils.django_db.backup`), mirroring the detect/execute split every other
mechanical scanner uses (``refresh_snapshot``, ``free_resources``). Best-effort:
any failure logs and is swallowed so a bad backup pass never aborts the loop tick.
"""

import logging
from pathlib import Path

from teatree.loop.dispatch import ActionPayload

logger = logging.getLogger(__name__)


def run_db_backup(payload: ActionPayload) -> None:
    """Take one control-DB backup + prune retention — best-effort, never raises into the loop.

    ``retention_days`` and ``backup_dir`` come from the scanner's signal payload
    (resolved from config at scan time), so the executor honours the same knobs the
    cadence gate read. Missing/invalid values fall back to the engine defaults.
    """
    retention_days = payload.get("retention_days")
    backup_dir_raw = payload.get("backup_dir")
    backup_dir = Path(backup_dir_raw) if isinstance(backup_dir_raw, str) and backup_dir_raw else None
    try:
        from teatree.utils.django_db.backup import run_backup  # noqa: PLC0415 deferred: DB-reaching engine

        result = run_backup(
            retention_days=retention_days if isinstance(retention_days, int) and retention_days > 0 else 7,
            backup_dir=backup_dir,
        )
    except Exception:
        logger.exception("run_db_backup: backup pass failed")
        return
    if result.created is not None:
        logger.info("run_db_backup: wrote %s (pruned %d expired)", result.created, len(result.pruned))
    else:
        logger.warning("run_db_backup: no backup written (%s); pruned %d", result.skipped_reason, len(result.pruned))


__all__ = ["run_db_backup"]
