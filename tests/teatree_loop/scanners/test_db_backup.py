"""DB-backup scanner: emits ``db_backup.due`` on the cadence, silent when fresh (directive #2)."""

from datetime import timedelta
from pathlib import Path
from unittest.mock import patch

import pytest
from django.utils import timezone

from teatree.loop.scanners.base import ScannerError, ScannerErrorClass
from teatree.loop.scanners.db_backup import DbBackupScanner


def _touch_backup(backup_dir: Path, *, age_hours: float) -> None:
    backup_dir.mkdir(parents=True, exist_ok=True)
    stamp = timezone.now() - timedelta(hours=age_hours)
    (backup_dir / f"db-{stamp.strftime('%Y%m%d-%H%M%S')}.sqlite3").write_bytes(b"stub")


class TestDbBackupScanner:
    def test_bootstrap_emits_when_no_backup_exists(self, tmp_path: Path) -> None:
        signals = DbBackupScanner(retention_days=7, cadence_hours=24, backup_dir=tmp_path).scan()
        assert len(signals) == 1
        assert signals[0].kind == "db_backup.due"
        assert signals[0].payload["trigger"] == "bootstrap"

    def test_fresh_backup_within_cadence_is_silent(self, tmp_path: Path) -> None:
        _touch_backup(tmp_path, age_hours=1)
        signals = DbBackupScanner(retention_days=7, cadence_hours=24, backup_dir=tmp_path).scan()
        assert signals == []

    def test_stale_backup_past_cadence_emits_cadence_trigger(self, tmp_path: Path) -> None:
        _touch_backup(tmp_path, age_hours=48)
        signals = DbBackupScanner(retention_days=7, cadence_hours=24, backup_dir=tmp_path).scan()
        assert len(signals) == 1
        assert signals[0].payload["trigger"] == "cadence"

    def test_signal_carries_retention_and_backup_dir(self, tmp_path: Path) -> None:
        signals = DbBackupScanner(retention_days=14, cadence_hours=24, backup_dir=tmp_path).scan()
        payload = signals[0].payload
        assert payload["retention_days"] == 14
        assert payload["backup_dir"] == str(tmp_path)

    def test_an_unreadable_backup_dir_raises_instead_of_going_silent(self, tmp_path: Path) -> None:
        """A dir read failure must reach the dispatcher's alarm, not disappear as 'no backup due'.

        Swallowing it into ``[]`` is indistinguishable from a genuinely fresh backup, so the
        directive's own retention guarantee could lapse for good with nothing ever telling the
        owner — the ``ScannerError`` path is what wires this scanner into the existing
        DM-once-per-day escalation every other scanner already gets on a recoverable failure.
        """
        with (
            patch(
                "teatree.loop.scanners.db_backup.hours_since_last_backup",
                side_effect=OSError("permission denied"),
            ),
            pytest.raises(ScannerError) as exc_info,
        ):
            DbBackupScanner(retention_days=7, cadence_hours=24, backup_dir=tmp_path).scan()
        assert exc_info.value.scanner == "db_backup"
        assert exc_info.value.error_class is ScannerErrorClass.UNKNOWN
