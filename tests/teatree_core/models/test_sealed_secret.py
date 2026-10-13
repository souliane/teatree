import tempfile
from pathlib import Path

import pytest
from django.db import IntegrityError, transaction
from django.test import TestCase

from teatree.core.models.sealed_secret import SealedSecret
from teatree.core.sealed_store import DbStore
from teatree.utils.master_key import KeyFileSource
from teatree.utils.sealing import SealedValueTamperedError


class TestSealedSecret(TestCase):
    def setUp(self) -> None:
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.source = KeyFileSource(Path(temporary.name) / "master.key")
        self.store = DbStore(self.source)

    def test_absent_read_does_not_create_key(self) -> None:
        assert self.store.read("alpha/token") is None
        assert not self.source.path.exists()

    def test_store_roundtrips_with_same_bytes_at_new_source(self) -> None:
        assert self.store.write("alpha/token", "canary") is True
        assert self.source.path.stat().st_mode & 0o777 == 0o600
        copy = self.source.path.with_name("copy.key")
        copy.write_bytes(self.source.path.read_bytes() + b"\n")
        copy.chmod(0o600)
        assert DbStore(KeyFileSource(copy)).read("alpha/token") == "canary"
        assert SealedSecret.objects.get(name="alpha/token").updated_at

    def test_existing_rows_refuse_missing_key_without_creating_one(self) -> None:
        self.store.write("alpha/token", "canary")
        self.source.path.unlink()
        with pytest.raises(ValueError, match="unreadable"):
            self.store.write("beta/token", "another")
        assert not self.source.path.exists()
        assert not SealedSecret.objects.filter(name="beta/token").exists()

    def test_swapping_ciphertext_between_names_is_tampering(self) -> None:
        self.store.write("alpha/token", "first")
        self.store.write("beta/token", "second")
        other = SealedSecret.objects.get(name="beta/token")
        SealedSecret.objects.filter(name="alpha/token").update(nonce=other.nonce, ciphertext=other.ciphertext)
        with pytest.raises(SealedValueTamperedError):
            self.store.read("alpha/token")

    def test_remove_reports_absence(self) -> None:
        assert self.store.remove("alpha/token") is False
        self.store.write("alpha/token", "canary")
        assert self.store.remove("alpha/token") is True
        assert self.store.remove("alpha/token") is False

    def test_database_rejects_malformed_sealed_rows(self) -> None:
        malformed = (
            {"name": "", "key_id": "a" * 16, "nonce": b"n" * 12, "ciphertext": b"c" * 16},
            {"name": "bad/key-id", "key_id": "short", "nonce": b"n" * 12, "ciphertext": b"c" * 16},
            {"name": "bad/nonce", "key_id": "a" * 16, "nonce": b"n", "ciphertext": b"c" * 16},
            {"name": "bad/ciphertext", "key_id": "a" * 16, "nonce": b"n" * 12, "ciphertext": b"c"},
        )
        for row in malformed:
            with self.subTest(name=row["name"]), transaction.atomic(), pytest.raises(IntegrityError):
                SealedSecret.objects.create(**row)
