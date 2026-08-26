"""The doctor's SQLite integrity gate — the one check that must not be read-only.

Two corruptions reached production undetected because every doctor probe read, and reads
succeed on a corrupt b-tree. The gate runs ``PRAGMA quick_check`` through Django's own
connection; these tests exercise it against the live test database and against a genuinely
corrupt file, so a refactor that stops actually running the PRAGMA goes red.
"""

import json
import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from unittest.mock import patch

import pytest
from django.conf import settings
from django.db import connections
from django.db.utils import ConnectionHandler
from django.test import TestCase
from pytest_django.plugin import DjangoDbBlocker

from teatree.cli.doctor import checks_db_integrity
from teatree.cli.doctor.checks_db_integrity import (
    _SQLITE_OK,
    _check_db_integrity,
    _check_db_is_off_the_host_filesystem,
    _check_host_projection_is_current,
    _check_no_host_process_holds_the_db_writable,
    _quick_check,
)
from teatree.config.host_projection import GENERATION_KEY, GLOBAL_SCOPE, ProjectionPublisher, ProjectionReader
from teatree.paths import CONTROL_DB_DIR_ENV
from teatree.settings import SQLITE_BOUNDARY_ENGINE, SQLITE_WRITE_SERIALIZATION_OPTIONS


def _sound_db(path: Path) -> Path:
    conn = sqlite3.connect(path)
    try:
        conn.execute("CREATE TABLE t(id INTEGER PRIMARY KEY AUTOINCREMENT, v TEXT)")
        conn.executemany("INSERT INTO t(v) VALUES(?)", [(f"row-{i}",) for i in range(200)])
        conn.execute("CREATE INDEX t_v_idx ON t(v)")
        conn.commit()
    finally:
        conn.close()
    return path


def _corrupt_in_place(path: Path) -> Path:
    """Overwrite a page mid-file so the b-tree no longer parses."""
    with path.open("r+b") as handle:
        handle.seek(path.stat().st_size // 2)
        handle.write(b"\xde\xad\xbe\xef" * 512)
    return path


def _pragma_rows(path: Path) -> list[str]:
    conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    try:
        return [str(row[0]) for row in conn.execute("PRAGMA quick_check")]
    except sqlite3.DatabaseError as corrupt:
        return [str(corrupt)]
    finally:
        conn.close()


class TestQuickCheckAgainstTheLiveConnection(TestCase):
    def test_the_test_database_is_sound(self) -> None:
        """The gate must pass a healthy database, or it would fail every doctor run."""
        assert _quick_check() == []

    def test_a_sound_database_does_not_fail_the_doctor(self) -> None:
        assert _check_db_integrity() is True


class TestDetectionActuallyWorks:
    """The anti-vacuous half: a mocked PRAGMA would pass the tests above while the gate rots."""

    def test_a_corrupt_file_is_not_reported_sound(self, tmp_path: Path) -> None:
        corrupt = _corrupt_in_place(_sound_db(tmp_path / "corrupt.sqlite3"))

        assert _pragma_rows(corrupt) != [_SQLITE_OK], "corruption must be reported, not swallowed"

    def test_a_sound_file_reports_exactly_ok(self, tmp_path: Path) -> None:
        assert _pragma_rows(_sound_db(tmp_path / "sound.sqlite3")) == [_SQLITE_OK]


def _databases(db: Path, engine: str) -> dict[str, dict[str, object]]:
    return {"default": {"ENGINE": engine, "NAME": str(db), "OPTIONS": SQLITE_WRITE_SERIALIZATION_OPTIONS}}


@contextmanager
def _database_config(db: Path, engine: str) -> Iterator[None]:
    """Point ``settings.DATABASES`` at *db* without ``override_settings``.

    ``override_settings(DATABASES=…)`` warns (a COMPLEX_OVERRIDE_SETTING) and the
    suite turns warnings into errors; the check only READS the mapping to find the
    file it should interrogate, so patching it in place is both quieter and closer
    to what the check actually consumes.
    """
    with patch.dict(settings.DATABASES, _databases(db, engine), clear=True):
        yield


@contextmanager
def _guarded_default_connection(db: Path, blocker: DjangoDbBlocker) -> Iterator[None]:
    """Make ``connections['default']`` a real guarded connection to *db*, then restore it.

    The check interrogates the live default connection on purpose (one connection
    lifecycle, not two), so exercising its verdict means substituting that connection
    rather than mocking the PRAGMA it reads.
    """
    handler = ConnectionHandler(_databases(db, SQLITE_BOUNDARY_ENGINE))
    original = connections["default"]
    connections["default"] = handler["default"]
    try:
        with blocker.unblock():
            yield
    finally:
        connections["default"] = original
        handler.close_all()


class TestTheDatabaseIsOffTheHostFilesystem:
    """The check the claim file could not make.

    Its predecessor asserted a marker existed beside the database, and stayed GREEN
    through five corruptions: write access is granted once at connection setup and
    never revoked, so a descriptor opened before the marker appeared outlives it.
    """

    def test_a_database_in_the_control_volume_passes(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        volume = tmp_path / "control-db"
        volume.mkdir()
        monkeypatch.setenv(CONTROL_DB_DIR_ENV, str(volume))
        monkeypatch.setattr(checks_db_integrity, "TRUE_CANONICAL_DB", _sound_db(volume / "db.sqlite3"))

        assert _check_db_is_off_the_host_filesystem() is True
        assert "OK" in capsys.readouterr().out

    def test_a_database_still_in_the_bind_mounted_data_dir_fails(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        monkeypatch.setenv(CONTROL_DB_DIR_ENV, str(tmp_path / "control-db"))
        monkeypatch.setattr(checks_db_integrity, "TRUE_CANONICAL_DB", _sound_db(tmp_path / "db.sqlite3"))

        assert _check_db_is_off_the_host_filesystem() is False
        assert "host-reachable filesystem" in capsys.readouterr().out


@pytest.fixture
def _on_a_host(monkeypatch: pytest.MonkeyPatch) -> None:
    """Pin the HOST precondition, which is the whole subject of this check.

    ``foreign_writers`` returns nothing when it believes it is containerized — every
    visible process is then the stack itself — and the suite runs in a container in CI.
    Unpinned, the planted descriptor below would be reported as no writer at all, and
    the passing case would pass without ever looking.
    """
    monkeypatch.setattr("teatree.db.write_domain.is_running_in_container", lambda: False)


@pytest.mark.usefixtures("_on_a_host")
class TestNoHostProcessHoldsTheDatabaseWritable:
    """Planted against a REAL descriptor — the condition, not a proxy for it."""

    def test_an_unheld_database_passes(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        monkeypatch.setattr(checks_db_integrity, "TRUE_CANONICAL_DB", _sound_db(tmp_path / "db.sqlite3"))
        monkeypatch.setattr(checks_db_integrity, "DATA_DIR", tmp_path / "data")

        assert _check_no_host_process_holds_the_db_writable() is True
        assert "no process holds" in capsys.readouterr().out

    def test_a_live_read_write_descriptor_fails_and_names_the_holder(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        db = _sound_db(tmp_path / "db.sqlite3")
        monkeypatch.setattr(checks_db_integrity, "TRUE_CANONICAL_DB", db)
        monkeypatch.setattr(checks_db_integrity, "DATA_DIR", tmp_path / "data")

        with db.open("r+b"):
            assert _check_no_host_process_holds_the_db_writable() is False

        assert "hold" in capsys.readouterr().out

    def test_an_unreadable_descriptor_table_reports_unverified_rather_than_ok(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        monkeypatch.setattr(checks_db_integrity, "TRUE_CANONICAL_DB", _sound_db(tmp_path / "db.sqlite3"))
        monkeypatch.setattr(checks_db_integrity, "DATA_DIR", tmp_path / "data")
        monkeypatch.setattr("teatree.db.write_domain._PROC", tmp_path / "no-procfs")
        monkeypatch.setattr("teatree.db.write_domain._which", lambda _: None)

        assert _check_no_host_process_holds_the_db_writable() is True

        out = capsys.readouterr().out
        assert "UNVERIFIED" in out
        assert "no descriptor view" in out
        assert "OK    Control DB writers" not in out, "an unread table must never certify no writers"


class TestNoProcessHoldsAHostCopyOfTheControlDatabase:
    """The half the canonical-only probe could not reach.

    The canonical database lives in a volume with no host path, so on the host it
    never exists and the check early-returned OK — inert exactly where the damage
    happens. What host processes actually hold descriptors on are the copies left
    under the bind-mounted data dir, and a rename does not close a descriptor, so
    the file a writer holds may no longer be named ``db.sqlite3`` at all.
    """

    @staticmethod
    def _isolate(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
        """A data dir with no canonical database — the real host's shape."""
        data_dir = tmp_path / "data"
        data_dir.mkdir()
        monkeypatch.setattr(checks_db_integrity, "DATA_DIR", data_dir)
        monkeypatch.setattr(checks_db_integrity, "TRUE_CANONICAL_DB", tmp_path / "control-db" / "db.sqlite3")
        return data_dir

    def test_a_held_data_dir_database_fails_even_with_no_canonical_database(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        data_dir = self._isolate(tmp_path, monkeypatch)
        legacy = _sound_db(data_dir / "db.sqlite3")
        assert not (tmp_path / "control-db" / "db.sqlite3").exists(), "control: the volume is unreachable from here"

        with legacy.open("r+b"):
            assert _check_no_host_process_holds_the_db_writable() is False

        assert str(legacy) in capsys.readouterr().out

    def test_a_held_renamed_database_fails(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        # The measured shape: `t3 admin` kept fd 4u on `db.sqlite3.precorrupt-inode-…`
        # for five days. Renaming the file retired the NAME, never the descriptor.
        data_dir = self._isolate(tmp_path, monkeypatch)
        renamed = _sound_db(data_dir / "db.sqlite3.precorrupt-inode-20260727")

        with renamed.open("r+b"):
            assert _check_no_host_process_holds_the_db_writable() is False

        assert str(renamed) in capsys.readouterr().out

    def test_a_held_write_ahead_log_fails(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        data_dir = self._isolate(tmp_path, monkeypatch)
        wal = data_dir / "db.sqlite3-wal"
        wal.write_bytes(b"\x00" * 64)

        with wal.open("r+b"):
            assert _check_no_host_process_holds_the_db_writable() is False

        assert str(wal) in capsys.readouterr().out

    def test_a_read_only_descriptor_is_not_a_writer(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        data_dir = self._isolate(tmp_path, monkeypatch)
        legacy = _sound_db(data_dir / "db.sqlite3")

        with legacy.open("rb"):
            assert _check_no_host_process_holds_the_db_writable() is True

        assert "no process holds" in capsys.readouterr().out

    def test_an_unheld_data_dir_database_passes(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        data_dir = self._isolate(tmp_path, monkeypatch)
        _sound_db(data_dir / "db.sqlite3")

        assert _check_no_host_process_holds_the_db_writable() is True
        assert "no process holds" in capsys.readouterr().out


class TestTheHostProjectionIsCurrent:
    """The one place a publisher that has stopped is visible.

    A host cannot open the source at all, so only this side can compare the
    projection's generation against it — without that, a stale projection reads
    exactly like a fresh one and the hooks quietly serve old kill-switch values.
    """

    @staticmethod
    def _projectable(db: Path, generation: int) -> Path:
        """Give the sound database the two projected tables, at *generation*."""
        conn = sqlite3.connect(db)
        try:
            conn.executescript(
                "CREATE TABLE teatree_config_setting (scope TEXT, key TEXT, value TEXT, "
                "created_at TEXT, updated_at TEXT, UNIQUE (scope, key));"
                "CREATE TABLE teatree_loop_state (name TEXT UNIQUE, status TEXT);"
            )
            conn.execute(
                "INSERT INTO teatree_config_setting VALUES (?, ?, ?, '', '')",
                (GLOBAL_SCOPE, GENERATION_KEY, json.dumps(generation)),
            )
            conn.commit()
        finally:
            conn.close()
        return db

    @staticmethod
    def _set_generation(db: Path, generation: int) -> None:
        conn = sqlite3.connect(db)
        try:
            conn.execute(
                "UPDATE teatree_config_setting SET value=? WHERE scope=? AND key=?",
                (json.dumps(generation), GLOBAL_SCOPE, GENERATION_KEY),
            )
            conn.commit()
        finally:
            conn.close()

    def test_a_matching_generation_passes(self, tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
        db = self._projectable(_sound_db(tmp_path / "db.sqlite3"), 7)
        ProjectionPublisher(db, db.parent).publish()

        with _database_config(db, SQLITE_BOUNDARY_ENGINE):
            assert _check_host_projection_is_current() is True

        assert "current at generation 7" in capsys.readouterr().out

    def test_a_source_that_moved_past_the_projection_fails(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        db = self._projectable(_sound_db(tmp_path / "db.sqlite3"), 7)
        ProjectionPublisher(db, db.parent).publish()
        # The publisher stopped: the source ratchets on, the published file does not.
        self._set_generation(db, 42)

        with _database_config(db, SQLITE_BOUNDARY_ENGINE):
            assert _check_host_projection_is_current() is False

        assert "the source is at generation 42" in capsys.readouterr().out

    def test_an_absent_projection_fails(self, tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
        db = self._projectable(_sound_db(tmp_path / "db.sqlite3"), 3)
        assert ProjectionReader(db.parent).read().projection is None, "control: nothing is published"

        with _database_config(db, SQLITE_BOUNDARY_ENGINE):
            assert _check_host_projection_is_current() is False

        assert "nothing published" in capsys.readouterr().out


class TestWriterClassificationIsFalsifiable:
    """The writers gate is pinned at the OS read in the doctor smoke tests, so the
    classification it guards is proven here in BOTH directions — a stub that let the
    wrong answer pass would retire the flake without retiring the defect."""

    def test_a_holder_is_reported_and_fails(self, capsys: pytest.CaptureFixture[str]) -> None:
        holders = [(Path("/var/lib/teatree/control-db/db.sqlite3"), "python[5603] (rw)")]
        with patch.object(checks_db_integrity, "_control_db_writers", return_value=holders):
            assert _check_no_host_process_holds_the_db_writable() is False

        out = capsys.readouterr().out
        assert "FAIL" in out
        assert "python[5603] (rw)" in out
        assert "1 descriptor(s)" in out

    def test_no_holder_passes_and_says_so(self, capsys: pytest.CaptureFixture[str]) -> None:
        with patch.object(checks_db_integrity, "_control_db_writers", return_value=[]):
            assert _check_no_host_process_holds_the_db_writable() is True

        assert "OK" in capsys.readouterr().out
