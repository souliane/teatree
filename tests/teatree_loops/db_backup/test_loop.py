"""DB-backup mini-loop: cadence anchor + scanner fan-out (directive #2)."""

from unittest.mock import patch

from teatree.loop.scanners.db_backup import DbBackupScanner
from teatree.loops.db_backup.loop import MINI_LOOP, _build_jobs


class TestDbBackupMiniLoop:
    def test_name_and_daily_cadence(self) -> None:
        assert MINI_LOOP.name == "db_backup"
        assert MINI_LOOP.default_cadence_seconds == 86400

    def test_build_jobs_returns_the_scanner_when_enabled(self) -> None:
        scanner = DbBackupScanner(retention_days=7, cadence_hours=24)
        with patch("teatree.loop.global_scanner_factories._db_backup_scanner", return_value=scanner):
            jobs = _build_jobs()
        assert len(jobs) == 1
        assert jobs[0].scanner is scanner
        assert jobs[0].overlay == ""

    def test_build_jobs_empty_when_disabled(self) -> None:
        with patch("teatree.loop.global_scanner_factories._db_backup_scanner", return_value=None):
            assert _build_jobs() == []
