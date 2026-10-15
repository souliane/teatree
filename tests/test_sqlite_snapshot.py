import sqlite3
from contextlib import closing
from pathlib import Path

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
