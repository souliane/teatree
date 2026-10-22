import sqlite3
from contextlib import closing
from pathlib import Path
from unittest.mock import patch

import pytest

from teatree.sqlite_snapshot import _sqlite_snapshot, uncheckpointed_wal


def test_unreadable_wal_stat_fails_as_sqlite_error(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    db = tmp_path / "db.sqlite3"
    wal = Path(f"{db}-wal")
    original_stat = Path.stat

    def denied_stat(path: Path, *args: object, **kwargs: object) -> object:
        if path == wal:
            message = "permission denied"
            raise PermissionError(message)
        return original_stat(path, *args, **kwargs)

    monkeypatch.setattr(Path, "stat", denied_stat)
    with pytest.raises(sqlite3.OperationalError, match="Cannot inspect"):
        uncheckpointed_wal(db)


def test_snapshot_fails_when_wal_cannot_be_inspected(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    source = tmp_path / "source.sqlite3"
    wal = Path(f"{source}-wal")
    original_stat = Path.stat

    def denied_stat(path: Path, *args: object, **kwargs: object) -> object:
        if path == wal:
            message = "permission denied"
            raise PermissionError(message)
        return original_stat(path, *args, **kwargs)

    def refuse_readonly(path: str | Path, *, uri: bool = False) -> sqlite3.Connection:
        message = "unable to open database file"
        raise sqlite3.OperationalError(message)

    monkeypatch.setattr(Path, "stat", denied_stat)
    with (
        patch("teatree.sqlite_snapshot.sqlite3.connect", side_effect=refuse_readonly),
        pytest.raises(sqlite3.OperationalError, match="Cannot inspect"),
    ):
        _sqlite_snapshot(source, tmp_path / "copy.sqlite3")


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


@pytest.mark.parametrize("destination", ["new", "existing"])
def test_snapshot_refuses_immutable_fallback_with_uncheckpointed_wal(tmp_path: Path, destination: str) -> None:
    source_dir = tmp_path / "source"
    source_dir.mkdir()
    source = source_dir / "db.sqlite3"
    dest = tmp_path / "copy.sqlite3"
    if destination == "existing":
        dest.write_bytes(b"existing copy")
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
        if destination == "existing":
            assert dest.read_bytes() == b"existing copy"
        else:
            assert not dest.exists()
    finally:
        writer.close()
