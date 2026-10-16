import sqlite3
from contextlib import closing
from pathlib import Path
from unittest.mock import patch

import pytest

from teatree.sqlite_snapshot import _sqlite_snapshot


def test_wal_snapshot_is_a_standalone_rollback_file(tmp_path: Path) -> None:
    source = tmp_path / "source.sqlite3"
    dest = tmp_path / "snapshot.sqlite3"
    writer = sqlite3.connect(source)
    try:
        assert writer.execute("PRAGMA journal_mode=WAL").fetchone() == ("wal",)
        writer.execute("PRAGMA wal_autocheckpoint=0")
        writer.execute("CREATE TABLE note(body TEXT)")
        writer.execute("INSERT INTO note VALUES ('in-wal')")
        writer.commit()
        assert Path(f"{source}-wal").stat().st_size > 0

        _sqlite_snapshot(source, dest)

        assert dest.read_bytes()[18] == 1
        assert not Path(f"{dest}-wal").exists()
        assert not Path(f"{dest}-shm").exists()
        with closing(sqlite3.connect(dest)) as copy:
            assert copy.execute("SELECT body FROM note").fetchall() == [("in-wal",)]
            assert copy.execute("PRAGMA quick_check").fetchone() == ("ok",)
    finally:
        writer.close()


def test_snapshot_refuses_immutable_fallback_with_uncheckpointed_wal(tmp_path: Path) -> None:
    source_dir = tmp_path / "source"
    source_dir.mkdir()
    source = source_dir / "db.sqlite3"
    dest = tmp_path / "copy.sqlite3"
    writer = sqlite3.connect(source)
    try:
        assert writer.execute("PRAGMA journal_mode=WAL").fetchone() == ("wal",)
        writer.execute("PRAGMA wal_autocheckpoint=0")
        writer.execute("CREATE TABLE note(body TEXT)")
        writer.execute("INSERT INTO note VALUES ('in-wal')")
        writer.commit()
        wal = Path(f"{source}-wal")
        assert wal.stat().st_size > 0
        connect = sqlite3.connect

        def refuse_readonly(path: str | Path, *, uri: bool = False) -> sqlite3.Connection:
            if "mode=ro" in str(path):
                message = "unable to open database file"
                raise sqlite3.OperationalError(message)
            return connect(path, uri=uri)

        with (
            patch("teatree.sqlite_snapshot.sqlite3.connect", side_effect=refuse_readonly),
            pytest.raises(sqlite3.OperationalError, match="-wal"),
        ):
            _sqlite_snapshot(source, dest)
        assert not dest.exists()
    finally:
        writer.close()
