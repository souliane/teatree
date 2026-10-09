"""Periodic control-DB backup scanner (directive #2).

The user directive: "daily DB backup, keep last N days." This is the local half —
the ``db_backup`` mini-loop fires this scanner, which cadence-gates on the newest
backup artifact and emits ``db_backup.due`` when a fresh backup is owed. The
``run_db_backup`` mechanical handler (:mod:`teatree.loop.mechanical_db_backup`)
then drives the shared engine (:mod:`teatree.utils.django_db.backup`) — the actual
snapshot + retention prune — off the tick.

The scanner mirrors :class:`~teatree.loop.scanners.eval_local.EvalLocalScanner`:

* **The row is the cadence.** ``[loops.db_backup]``'s 02:00 anchor decides WHEN; the
    scanner does not hold a second daily clock, because two daily clocks in series drift
    the backup later every day rather than keeping it daily.
* **No new marker.** The newest artifact's OWN embedded timestamp is the "last
    backup" clock (:func:`teatree.utils.django_db.backup.hours_since_last_backup`); no model
    row is added. No prior backup ⇒ a ``bootstrap`` trigger fires the first one.
* **Config is injected, not read at scan time.** The wiring layer
    (:func:`teatree.loop.global_scanner_factories._db_backup_scanner`) resolves
    :class:`teatree.config.UserSettings`; whether the loop runs at all is the
    ``db_backup`` ``Loop`` row's preset opinion, and the scanner itself always flags a
    due backup when invoked.
* **Non-blocking split.** ``scan()`` only reads the dir and returns a signal; the
    mechanical handler does the snapshot, mirroring the detect/execute split every
    other mechanical scanner uses.
"""

from dataclasses import dataclass
from pathlib import Path

from django.utils import timezone

from teatree.loop.scanners.base import ScannerError, ScannerErrorClass, ScanSignal
from teatree.utils.django_db.backup import default_backup_dir, hours_since_last_backup


@dataclass(slots=True)
class DbBackupScanner:
    """Emit ``db_backup.due`` whenever the loop row ticks it — the row IS the cadence."""

    retention_days: int
    backup_dir: Path | None = None
    name: str = "db_backup"

    def scan(self) -> list[ScanSignal]:
        target_dir = self.backup_dir if self.backup_dir is not None else default_backup_dir()
        now = timezone.now()
        try:
            elapsed_hours = hours_since_last_backup(target_dir, now=now)
        except Exception as exc:
            # A swallowed [] reads identically to "no backup due" — the retention
            # directive could lapse for good with nothing ever telling the owner.
            raise ScannerError(scanner=self.name, error_class=ScannerErrorClass.UNKNOWN, detail=str(exc)) from exc

        return [
            ScanSignal(
                kind="db_backup.due",
                summary=f"control-DB backup due (trigger: {_trigger(elapsed_hours)}) "
                f"— keeping last {self.retention_days}d",
                payload={
                    "trigger": _trigger(elapsed_hours),
                    "retention_days": self.retention_days,
                    "backup_dir": str(target_dir),
                },
            ),
        ]


def _trigger(elapsed_hours: float | None) -> str:
    """Names WHY this backup is owed — the first one ever reads differently from the daily one."""
    return "bootstrap" if elapsed_hours is None else "cadence"


__all__ = ["DbBackupScanner"]
