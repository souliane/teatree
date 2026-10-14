import base64
import tempfile
from pathlib import Path
from unittest.mock import patch

import pytest
from django.db import IntegrityError, transaction
from django.test import TestCase

from teatree.core.models.sealed_secret import SealedSecret, SealedStoreReadBackError
from teatree.core.sealed.store import DbStore
from teatree.utils.master_key import KeyFileSource
from teatree.utils.sealing import SealedValueTamperedError
from teatree.utils.secrets import SecretStoreError


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
        assert len(base64.b64decode(self.source.path.read_text(), validate=True)) == 32
        before = self._row_bytes("alpha/token")
        copy = self.source.path.with_name("copy.key")
        copy.write_bytes(self.source.path.read_bytes() + b"\n")
        copy.chmod(0o600)
        assert DbStore(KeyFileSource(copy)).read("alpha/token") == "canary"
        assert self._row_bytes("alpha/token") == before

    def test_first_write_warns_once_with_key_id(self) -> None:
        with self.assertLogs("teatree.utils.master_key", "WARNING") as logs:
            self.store.write("alpha/token", "canary")
        (message,) = logs.output
        assert self.source.load().key_id in message
        assert "keep a copy of this key" in message
        assert self.source.path.read_text() not in message
        with self.assertNoLogs("teatree.utils.master_key", "WARNING"):
            self.store.write("beta/token", "another")

    @staticmethod
    def _row_bytes(name: str) -> tuple[object, ...]:
        row = SealedSecret.objects.get(name=name)
        return (row.key_id, bytes(row.nonce), bytes(row.ciphertext), row.updated_at)

    def test_unicode_roundtrip_and_failed_read_back_preserves_timestamp(self) -> None:
        assert self.store.write("alpha/token", "pässwörd") is True
        before = SealedSecret.objects.get(name="alpha/token").updated_at
        assert self.store.read("alpha/token") == "pässwörd"
        with (
            patch.object(SealedSecret.objects, "reveal", return_value="wrong"),
            pytest.raises(SealedStoreReadBackError),
        ):
            self.store.write("alpha/token", "replacement")
        row = SealedSecret.objects.get(name="alpha/token")
        assert row.updated_at == before
        assert row.reveal(self.source.load()) == "pässwörd"

    def test_prefix_listing_is_case_sensitive(self) -> None:
        self.store.write("Anthropic/token", "upper")
        self.store.write("anthropic/token", "lower")
        assert self.store.names("anthropic") == frozenset({"token"})

    def test_existing_rows_refuse_missing_key_without_creating_one(self) -> None:
        self.store.write("alpha/token", "canary")
        self.source.path.unlink()
        with pytest.raises(SecretStoreError, match="unreadable"):
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
