"""Control-DB backup engine: real SQLite snapshot + keep-last-N-days retention (directive #2)."""

import sqlite3
from datetime import UTC, datetime, timedelta
from pathlib import Path
from unittest.mock import patch

import pytest
from django.db import connection

from teatree.utils.django_db import backup


def _make_db(path: Path, *, rows: list[str]) -> None:
    """Create a real SQLite DB at *path* with a ``note`` table holding *rows*."""
    conn = sqlite3.connect(path)
    try:
        conn.execute("CREATE TABLE note (body TEXT)")
        conn.executemany("INSERT INTO note (body) VALUES (?)", [(r,) for r in rows])
        conn.commit()
    finally:
        conn.close()


def _read_rows(path: Path) -> list[str]:
    conn = sqlite3.connect(path)
    try:
        return [r[0] for r in conn.execute("SELECT body FROM note ORDER BY body")]
    finally:
        conn.close()


def _touch_artifact(backup_dir: Path, stamp: datetime) -> Path:
    backup_dir.mkdir(parents=True, exist_ok=True)
    artifact = backup_dir / f"db-{stamp.strftime('%Y%m%d-%H%M%S')}.sqlite3"
    artifact.write_bytes(b"stub")
    return artifact


class TestArtifactTimestamp:
    def test_parses_embedded_utc_timestamp(self) -> None:
        parsed = backup.artifact_timestamp("db-20260115-093000.sqlite3")
        assert parsed == datetime(2026, 1, 15, 9, 30, 0, tzinfo=UTC)

    def test_rejects_foreign_filename(self) -> None:
        assert backup.artifact_timestamp("db.sqlite3") is None
        assert backup.artifact_timestamp("notes.txt") is None
        assert backup.artifact_timestamp("db-2026.sqlite3") is None


class TestCreateBackup:
    def test_snapshots_source_contents(self, tmp_path: Path) -> None:
        source = tmp_path / "db.sqlite3"
        _make_db(source, rows=["alpha", "beta"])
        now = datetime(2026, 1, 15, 9, 30, 0, tzinfo=UTC)

        dest = backup.create_backup(source=source, backup_dir=tmp_path / "backups", now=now)

        assert dest.name == "db-20260115-093000.sqlite3"
        assert dest.is_file()
        assert _read_rows(dest) == ["alpha", "beta"]

    def test_leaves_no_partial_temp_file(self, tmp_path: Path) -> None:
        source = tmp_path / "db.sqlite3"
        _make_db(source, rows=["x"])
        backups = tmp_path / "backups"
        backup.create_backup(source=source, backup_dir=backups, now=datetime.now(tz=UTC))
        assert not list(backups.glob(".*.partial"))


class TestExistingBackupsAndCadence:
    def test_newest_backup_at_returns_latest_embedded_stamp(self, tmp_path: Path) -> None:
        _touch_artifact(tmp_path, datetime(2026, 1, 10, 0, 0, 0, tzinfo=UTC))
        newest = datetime(2026, 1, 12, 6, 0, 0, tzinfo=UTC)
        _touch_artifact(tmp_path, newest)
        assert backup.newest_backup_at(tmp_path) == newest

    def test_newest_backup_at_none_when_empty(self, tmp_path: Path) -> None:
        assert backup.newest_backup_at(tmp_path) is None

    def test_hours_since_last_backup(self, tmp_path: Path) -> None:
        stamp = datetime(2026, 1, 12, 0, 0, 0, tzinfo=UTC)
        _touch_artifact(tmp_path, stamp)
        now = stamp + timedelta(hours=30)
        assert backup.hours_since_last_backup(tmp_path, now=now) == pytest.approx(30.0)

    def test_hours_since_last_backup_none_when_empty(self, tmp_path: Path) -> None:
        assert backup.hours_since_last_backup(tmp_path, now=datetime.now(tz=UTC)) is None

    def test_existing_backups_ignores_foreign_files(self, tmp_path: Path) -> None:
        _touch_artifact(tmp_path, datetime(2026, 1, 12, 0, 0, 0, tzinfo=UTC))
        (tmp_path / "db.sqlite3").write_bytes(b"live")
        (tmp_path / "README.txt").write_text("hi")
        assert [p.name for p in backup.existing_backups(tmp_path)] == ["db-20260112-000000.sqlite3"]


class TestPruneExpired:
    def test_deletes_older_than_retention_keeps_recent(self, tmp_path: Path) -> None:
        now = datetime(2026, 1, 20, 0, 0, 0, tzinfo=UTC)
        old = _touch_artifact(tmp_path, now - timedelta(days=10))
        recent = _touch_artifact(tmp_path, now - timedelta(days=2))

        pruned = backup.prune_expired(backup_dir=tmp_path, retention_days=7, now=now)

        assert pruned == [old]
        assert not old.exists()
        assert recent.exists()

    def test_never_touches_a_foreign_file(self, tmp_path: Path) -> None:
        now = datetime(2026, 1, 20, 0, 0, 0, tzinfo=UTC)
        _touch_artifact(tmp_path, now - timedelta(days=99))
        live = tmp_path / "db.sqlite3"
        live.write_bytes(b"live")

        backup.prune_expired(backup_dir=tmp_path, retention_days=7, now=now)

        assert live.exists()


class TestRunBackup:
    def test_creates_and_prunes_in_one_pass(self, tmp_path: Path) -> None:
        source = tmp_path / "db.sqlite3"
        _make_db(source, rows=["live"])
        backups = tmp_path / "backups"
        now = datetime(2026, 1, 20, 0, 0, 0, tzinfo=UTC)
        expired = _touch_artifact(backups, now - timedelta(days=30))

        result = backup.run_backup(retention_days=7, backup_dir=backups, source=source, now=now)

        assert result.created is not None
        assert result.created.is_file()
        assert _read_rows(result.created) == ["live"]
        assert result.pruned == [expired]
        assert not expired.exists()
        assert result.skipped_reason is None

    def test_skips_snapshot_when_no_source_but_still_prunes(self, tmp_path: Path) -> None:
        backups = tmp_path / "backups"
        now = datetime(2026, 1, 20, 0, 0, 0, tzinfo=UTC)
        expired = _touch_artifact(backups, now - timedelta(days=30))

        with patch.object(backup, "resolve_source_db", return_value=None):
            result = backup.run_backup(retention_days=7, backup_dir=backups, now=now)

        assert result.created is None
        assert result.skipped_reason is not None
        assert result.pruned == [expired]
        assert not expired.exists()


class TestResolveSourceDb:
    def test_returns_none_for_in_memory_db(self) -> None:
        with patch.dict(connection.settings_dict, {"NAME": ":memory:"}):
            assert backup.resolve_source_db() is None

    def test_returns_path_for_existing_file_db(self, tmp_path: Path) -> None:
        live = tmp_path / "db.sqlite3"
        live.write_bytes(b"live")
        with patch.dict(connection.settings_dict, {"NAME": str(live)}):
            assert backup.resolve_source_db() == live
