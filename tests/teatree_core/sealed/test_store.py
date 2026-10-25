import base64
import datetime as dt
import os
import subprocess
import sys
import tempfile
from collections.abc import Callable, Generator, Iterator
from contextlib import contextmanager
from pathlib import Path
from unittest.mock import patch

import pytest
from django.db import connections
from django.db.utils import OperationalError
from django.test import TestCase, override_settings

from teatree.core.models.sealed_secret import SealedSecret
from teatree.core.sealed.store import DbStore
from teatree.settings import SQLITE_WRITE_SERIALIZATION_OPTIONS
from teatree.utils.master_key import KeyFileSource
from teatree.utils.secrets import (
    PassStore,
    SecretNotFoundError,
    SecretStore,
    SecretStoreError,
    pass_entry_exists,
    pass_entry_names,
    read_pass,
    read_pass_required,
    remove_pass,
    use_store,
    write_pass,
    write_pass_with_backup,
)
from tests.db_alias import RouteAllToAlias, register_migrated_sqlite_alias, teardown_sqlite_alias
from tests.secret_memory_store import MemoryStore

FAKE_PASS = """#!/bin/sh
entry() { printf '%s/%s.gpg' "$PASSWORD_STORE_DIR" "$1"; }
case "$1" in
    show) [ -f "$(entry "$2")" ] || exit 1; cat "$(entry "$2")" ;;
    insert) mkdir -p "$(dirname "$(entry "$4")")"; cat > "$(entry "$4")" ;;
    rm) [ -f "$(entry "$3")" ] || exit 1; rm "$(entry "$3")" ;;
    *) exit 2 ;;
esac
"""

BREAKAGES = ("key removed", "key mode 0644", "wrong key", "tampered ciphertext")


@contextmanager
def fake_pass_store(root: Path) -> Generator[PassStore]:
    binary = root / "bin"
    binary.mkdir()
    (binary / "pass").write_text(FAKE_PASS)
    (binary / "pass").chmod(0o700)
    (root / "store").mkdir()
    with patch.dict(os.environ, {"PATH": f"{binary}:{os.environ['PATH']}", "PASSWORD_STORE_DIR": str(root / "store")}):
        yield PassStore()


class TestSecretStoreContract(TestCase):
    def _assert_contract(self, store: SecretStore) -> None:
        with use_store(store):
            assert read_pass("contract/token") == ""
            assert pass_entry_exists("contract/token") is False
            with pytest.raises(SecretNotFoundError):
                read_pass_required("contract/token")
            assert remove_pass("contract/token") is False
            assert write_pass("contract/token", "\n  pässwörd\nmetadata\n") is True
            assert read_pass("contract/token") == "pässwörd"
            assert read_pass_required("contract/token") == "pässwörd"
            assert pass_entry_exists("contract/token") is True
            assert pass_entry_names("contract") == frozenset({"token"})
            assert write_pass("contract/nested/deep", "x") is True
            assert write_pass("top", "y") is True
            assert pass_entry_names("contract") == frozenset({"token"})
            assert pass_entry_names("") == frozenset({"top"})
            assert remove_pass("contract/nested/deep") is True
            assert remove_pass("top") is True
            assert remove_pass("contract/token") is True
            assert remove_pass("contract/token") is False
            assert read_pass("contract/token") == ""

    def test_memory_store(self) -> None:
        self._assert_contract(MemoryStore())

    def test_pass_store_over_a_fake_pass(self) -> None:
        with tempfile.TemporaryDirectory() as temporary, fake_pass_store(Path(temporary)) as store:
            self._assert_contract(store)

    def test_database_store(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            self._assert_contract(DbStore(KeyFileSource(Path(temporary) / "master.key")))

    def test_database_write_failure_is_typed(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            store = DbStore(KeyFileSource(Path(temporary) / "master.key"))
            with (
                patch.object(SealedSecret.objects, "store", side_effect=OperationalError("database is locked")),
                pytest.raises(SecretStoreError, match="could not be written"),
            ):
                store.write("team/token", "value")


class _RefusingMemoryStore(MemoryStore):
    def write(self, key: str, value: str) -> bool:
        return False


class _BackupRaisingStore(MemoryStore):
    def write(self, key: str, value: str) -> bool:
        if ".bak-" in key:
            message = "database is locked"
            raise SecretStoreError(message)
        return super().write(key, value)


class TestFailingWriteIsRefusedTheSameWay(TestCase):
    def _assert_refused(self, store: SecretStore) -> None:
        with use_store(store), pytest.raises(SecretStoreError):
            write_pass_with_backup("contract/token", "value", echo=lambda _: None)
        with use_store(store):
            assert read_pass("contract/token") == ""

    def test_memory_store(self) -> None:
        self._assert_refused(_RefusingMemoryStore())

    def test_pass_store_over_a_fake_pass(self) -> None:
        with tempfile.TemporaryDirectory() as temporary, fake_pass_store(Path(temporary)) as store:
            (Path(temporary) / "bin" / "pass").write_text(FAKE_PASS.replace("insert) mkdir", "insert) exit 1; mkdir"))
            self._assert_refused(store)

    def test_database_store_with_an_uncreatable_key(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            self._assert_refused(DbStore(KeyFileSource(Path(temporary) / "missing" / "master.key")))

    def test_a_backup_write_that_raises_is_reported_as_not_backed_up(self) -> None:
        store = _BackupRaisingStore()
        store.entries["contract/token"] = "previous"
        with use_store(store), pytest.raises(SecretStoreError, match="could not back up") as raised:
            write_pass_with_backup("contract/token", "value", echo=lambda _: None)
        assert isinstance(raised.value.__cause__, SecretStoreError)
        assert store.entries["contract/token"] == "previous"


class TestDbStoreIsTheSoleWriter(TestCase):
    def setUp(self) -> None:
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.source = KeyFileSource(Path(temporary.name) / "master.key")
        self.store = DbStore(self.source)

    def _replace_key(self) -> str:
        original = self.source.path.read_text()
        self.source.path.unlink()
        self.source.create()
        return original

    def _restore_key(self, text: str) -> None:
        self.source.path.unlink()
        self.source.path.write_text(text)
        self.source.path.chmod(0o600)

    def test_write_refuses_a_second_key_id(self) -> None:
        self.store.write("team/one", "v1")
        (first,) = SealedSecret.objects.key_census()
        original = self._replace_key()

        with pytest.raises(SecretStoreError, match="key_id"):
            self.store.write("team/two", "v2")
        with pytest.raises(SecretStoreError, match="key_id"):
            self.store.write("team/one", "v3")

        assert set(SealedSecret.objects.key_census()) == {first}
        self._restore_key(original)
        assert self.store.write("team/two", "v2") is True
        assert self.store.read("team/one") == "v1"
        assert set(SealedSecret.objects.key_census()) == {first}

    def test_name_with_nul_creates_no_key_file(self) -> None:
        with pytest.raises(ValueError, match="no whitespace"):
            self.store.write("team/o\0ne", "v")
        assert not self.source.path.exists()
        assert self.store.write("team/one", "v") is True
        assert self.source.path.exists()


NOW = dt.datetime(2026, 10, 25, 12, 0, tzinfo=dt.UTC)


def _backup(base: str, age: dt.timedelta) -> str:
    return f"{base}.bak-{(NOW - age).strftime('%Y%m%dT%H%M%SZ')}"


class TestBackupRowRetention(TestCase):
    def setUp(self) -> None:
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.store = DbStore(KeyFileSource(Path(temporary.name) / "master.key"))

    def _names(self) -> list[str]:
        return sorted(SealedSecret.objects.values_list("name", flat=True))

    def test_bak_rows_keep_newest_three_and_90_days(self) -> None:
        ancient = _backup("team/token", dt.timedelta(days=91))
        recent = [_backup("team/token", dt.timedelta(minutes=minutes)) for minutes in (50, 40, 30, 20, 10)]
        unparsable = "team/token.bak-not-a-stamp"
        neighbour = _backup("other/token", dt.timedelta(hours=1))
        with patch("django.utils.timezone.now", return_value=NOW):
            self.store.write("team/token", "live")
            self.store.write(neighbour, "other")
            self.store.write(unparsable, "kept")
            self.store.write(ancient, "expired")
            assert ancient not in self._names()
            for name in recent:
                self.store.write(name, "backup")

        assert self._names() == sorted(["team/token", neighbour, unparsable, *recent[2:]])
        assert self.store.read("team/token") == "live"


class TestDatabaseStoreFailuresAreErrors(TestCase):
    def _sealed_then_broken(self, breakage: str) -> KeyFileSource:
        SealedSecret.objects.all().delete()
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        source = KeyFileSource(Path(temporary.name) / "master.key")
        DbStore(source).write("contract/token", "canary")
        if breakage == "key removed":
            source.path.unlink()
        elif breakage == "key mode 0644":
            source.path.chmod(0o644)
        elif breakage == "wrong key":
            source.path.unlink()
            source.create()
        elif breakage == "invalid key":
            source.path.write_text("not base64")
        elif breakage == "tampered ciphertext":
            row = SealedSecret.objects.get(name="contract/token")
            flipped = bytearray(row.ciphertext)
            flipped[0] ^= 1
            SealedSecret.objects.filter(pk=row.pk).update(ciphertext=bytes(flipped))
        return source

    def test_readers_raise_instead_of_reading_absent(self) -> None:
        readers: tuple[Callable[[str], object], ...] = (read_pass, read_pass_required, pass_entry_exists)
        for breakage in BREAKAGES:
            source = self._sealed_then_broken(breakage)
            for reader in readers:
                with (
                    self.subTest(breakage=breakage, reader=reader.__name__),
                    use_store(DbStore(source)),
                    pytest.raises(SecretStoreError),
                ):
                    reader("contract/token")

    def test_key_health_classifies_by_exception_type(self) -> None:
        expected = {
            "key removed": "UNREADABLE",
            "key mode 0644": "UNREADABLE",
            "invalid key": "INVALID",
            "wrong key": "WRONG_KEY",
            "tampered ciphertext": "TAMPERED",
        }
        for breakage, status in expected.items():
            with self.subTest(breakage=breakage):
                source = self._sealed_then_broken(breakage)
                assert SealedSecret.objects.key_health(source).status == status


SCAN_ALIAS = "sealed_disk_scan"
CANARY = "disk-scan-canary-5052-ünïcode"


def _forms(value: str) -> tuple[bytes, ...]:
    raw = value.encode()
    return (raw, base64.b64encode(raw), base64.urlsafe_b64encode(raw), raw.hex().encode())


def _sqlite_bytes(directory: Path) -> bytes:
    return b"".join(path.read_bytes() for path in sorted(directory.glob("db.sqlite3*")))


@pytest.fixture
def file_backed_control_db(
    tmp_path: Path, django_db_blocker: pytest.FixtureRequest, migrated_db_template: Path | None
) -> Iterator[Path]:
    directory = tmp_path / "control"
    directory.mkdir()
    with django_db_blocker.unblock():
        register_migrated_sqlite_alias(
            SCAN_ALIAS,
            directory / "db.sqlite3",
            migrated_db_template,
            options=dict(SQLITE_WRITE_SERIALIZATION_OPTIONS),
        )
        try:
            with override_settings(DATABASE_ROUTERS=[RouteAllToAlias(SCAN_ALIAS)]):
                yield directory
        finally:
            teardown_sqlite_alias(SCAN_ALIAS)


def test_committed_write_leaves_no_plaintext_in_database_files(file_backed_control_db: Path, tmp_path: Path) -> None:
    DbStore(KeyFileSource(tmp_path / "master.key")).write("scan/token", CANARY)

    stored = _sqlite_bytes(file_backed_control_db)
    assert SealedSecret.objects.filter(name="scan/token").exists()
    assert not [form for form in _forms(CANARY) if form in stored]

    with connections[SCAN_ALIAS].cursor() as cursor:
        cursor.execute("CREATE TABLE plaintext_control (value TEXT)")
        cursor.execute("INSERT INTO plaintext_control (value) VALUES (%s)", [CANARY])
    assert CANARY.encode() in _sqlite_bytes(file_backed_control_db)


def test_shell_reader_matches_facade_on_committed_rows(file_backed_control_db: Path, tmp_path: Path) -> None:
    source = KeyFileSource(tmp_path / "master.key")
    reader = Path(__file__).resolve().parents[3] / "deploy" / "secret-read.sh"
    binary = tmp_path / "bin"
    binary.mkdir()
    pass_log = tmp_path / "pass-calls"
    fake_pass = binary / "pass"
    fake_pass.write_text('#!/bin/sh\nprintf "%s\\n" "$*" >> "$PASS_LOG"\nexit 2\n')
    fake_pass.chmod(0o755)
    environment = {
        **os.environ,
        "PATH": f"{binary}:{os.environ['PATH']}",
        "PASS_LOG": str(pass_log),
        "T3_CONFIG_DB": str(file_backed_control_db / "db.sqlite3"),
        "T3_MASTER_KEY_SOURCE": source.label,
        "T3_SECRET_STORE": "sealed",
        "T3_SECRET_PYTHON": sys.executable,
    }

    def shell_get(name: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run([str(reader), "get", name], env=environment, capture_output=True, text=True, check=False)

    with use_store(DbStore(source)):
        assert write_pass("parity/token", "live-value\nmetadata") is True
        assert read_pass("parity/token") == "live-value"
        answered = shell_get("parity/token")
        assert (answered.returncode, answered.stdout) == (0, "live-value\n")
        assert read_pass("parity/missing") == ""
        assert shell_get("parity/missing").returncode == 1
        source.path.unlink()
        with pytest.raises(SecretStoreError):
            read_pass("parity/token")
        assert shell_get("parity/token").returncode == 2
    assert not pass_log.exists()
