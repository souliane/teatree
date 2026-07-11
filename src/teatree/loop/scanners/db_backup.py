"""Periodic control-DB backup scanner (directive #2).

The user directive: "daily DB backup, keep last N days." This is the local half —
the ``db_backup`` mini-loop fires this scanner, which cadence-gates on the newest
backup artifact and emits ``db_backup.due`` when a fresh backup is owed. The
``run_db_backup`` mechanical handler (:mod:`teatree.loop.mechanical_db_backup`)
then drives the shared engine (:mod:`teatree.utils.django_db.backup`) — the actual
snapshot + retention prune — off the tick.

The scanner mirrors :class:`~teatree.loop.scanners.eval_local.EvalLocalScanner`:

* **Single trigger.** Only a cadence (``db_backup_cadence_hours``, default 24h) —
    a fixed-rate platform behaviour, not coupled to delivery velocity.
* **No new marker.** The newest artifact's OWN embedded timestamp is the "last
    backup" clock (:func:`teatree.utils.django_db.backup.hours_since_last_backup`); no model
    row is added. No prior backup ⇒ a ``bootstrap`` trigger fires the first one.
* **Config is injected, not read at scan time.** The wiring layer
    (:func:`teatree.loop.global_scanner_factories._db_backup_scanner`) resolves
    :class:`teatree.config.UserSettings` and the on/off ``db_backup_disabled``
    kill-switch; the scanner itself always evaluates its cadence when invoked.
* **Non-blocking split.** ``scan()`` only reads the dir and returns a signal; the
    mechanical handler does the snapshot, mirroring the detect/execute split every
    other mechanical scanner uses.
"""

import logging
from dataclasses import dataclass
from pathlib import Path

from django.utils import timezone

from teatree.loop.scanners.base import ScanSignal
from teatree.utils.django_db.backup import default_backup_dir, hours_since_last_backup

logger = logging.getLogger(__name__)


@dataclass(slots=True)
class DbBackupScanner:
    """Emit ``db_backup.due`` when the newest control-DB backup is older than the cadence."""

    retention_days: int
    cadence_hours: int = 24
    backup_dir: Path | None = None
    name: str = "db_backup"

    def scan(self) -> list[ScanSignal]:
        target_dir = self.backup_dir if self.backup_dir is not None else default_backup_dir()
        now = timezone.now()
        try:
            elapsed_hours = hours_since_last_backup(target_dir, now=now)
        except Exception:
            logger.exception("db_backup: reading the backup dir %s failed — skipping this tick", target_dir)
            return []

        trigger = self._evaluate_trigger(elapsed_hours)
        if trigger is None:
            return []
        return [
            ScanSignal(
                kind="db_backup.due",
                summary=f"control-DB backup due (trigger: {trigger}) — keeping last {self.retention_days}d",
                payload={
                    "trigger": trigger,
                    "retention_days": self.retention_days,
                    "backup_dir": str(target_dir),
                },
            ),
        ]

    def _evaluate_trigger(self, elapsed_hours: float | None) -> str | None:
        if elapsed_hours is None:
            return "bootstrap"
        if elapsed_hours >= self.cadence_hours:
            return "cadence"
        return None


__all__ = ["DbBackupScanner"]
