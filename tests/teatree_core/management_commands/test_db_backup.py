"""``t3 db_backup`` command: snapshots the control DB + prunes retention (directive #2)."""

import sqlite3
from datetime import UTC, datetime, timedelta
from pathlib import Path
from unittest.mock import patch

import pytest
from django.core.management import call_command

# ast-grep-ignore: ac-django-no-pytest-django-db
pytestmark = pytest.mark.django_db


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


class TestDbBackupCommand:
    def test_writes_a_backup_and_reports_it(self, tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
        source = _make_source(tmp_path)
        backups = tmp_path / "backups"
        with (
            patch("teatree.utils.django_db.backup.resolve_source_db", return_value=source),
            patch("teatree.utils.django_db.backup.default_backup_dir", return_value=backups),
        ):
            call_command("db_backup")
        assert list(backups.glob("db-*.sqlite3"))
        assert "backup written" in capsys.readouterr().out

    def test_retention_override_prunes_expired_artifacts(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        source = _make_source(tmp_path)
        backups = tmp_path / "backups"
        backups.mkdir()
        expired = backups / f"db-{(datetime.now(tz=UTC) - timedelta(days=5)).strftime('%Y%m%d-%H%M%S')}.sqlite3"
        expired.write_bytes(b"stub")
        with (
            patch("teatree.utils.django_db.backup.resolve_source_db", return_value=source),
            patch("teatree.utils.django_db.backup.default_backup_dir", return_value=backups),
        ):
            call_command("db_backup", "--retention-days", "1")
        assert not expired.exists()
        assert "pruned 1 backup" in capsys.readouterr().out
