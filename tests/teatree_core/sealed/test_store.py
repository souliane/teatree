import base64
import os
import tempfile
from collections.abc import Callable, Generator, Iterator
from contextlib import contextmanager
from pathlib import Path
from unittest.mock import patch

import pytest
from django.db import connections
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
