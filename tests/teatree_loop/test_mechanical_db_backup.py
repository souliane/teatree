"""DB-backup mechanical handler: drives the snapshot + prune engine, best-effort (directive #2)."""

import logging
import sqlite3
from pathlib import Path
from unittest.mock import patch

import pytest

from teatree.loop.dispatch_tables import MECHANICAL_BY_KIND
from teatree.loop.mechanical import HANDLERS, run_db_backup


def _make_source(tmp_path: Path) -> Path:
    source = tmp_path / "db.sqlite3"
    conn = sqlite3.connect(source)
    try:
        conn.execute("CREATE TABLE note (body TEXT)")
        conn.execute("INSERT INTO note (body) VALUES ('live')")
        conn.commit()
    finally:
        conn.close()
    return source


class TestRunDbBackupHandler:
    def test_registered_in_dispatch_and_handler_tables(self) -> None:
        assert MECHANICAL_BY_KIND["db_backup.due"] == ("mechanical", "run_db_backup")
        assert HANDLERS["run_db_backup"] is run_db_backup

    def test_writes_a_backup_into_the_payload_dir(self, tmp_path: Path) -> None:
        source = _make_source(tmp_path)
        backups = tmp_path / "backups"
        with patch("teatree.utils.django_db.backup.resolve_source_db", return_value=source):
            run_db_backup({"retention_days": 7, "backup_dir": str(backups)})
        assert list(backups.glob("db-*.sqlite3"))

    def test_missing_retention_falls_back_and_still_runs(self, tmp_path: Path) -> None:
        source = _make_source(tmp_path)
        backups = tmp_path / "backups"
        with patch("teatree.utils.django_db.backup.resolve_source_db", return_value=source):
            run_db_backup({"backup_dir": str(backups)})
        assert list(backups.glob("db-*.sqlite3"))

    def test_engine_failure_is_swallowed_and_logged(self, caplog: pytest.LogCaptureFixture) -> None:
        with (
            patch("teatree.utils.django_db.backup.run_backup", side_effect=RuntimeError("boom")),
            caplog.at_level(logging.ERROR),
        ):
            run_db_backup({"retention_days": 7})
        assert "backup pass failed" in caplog.text
