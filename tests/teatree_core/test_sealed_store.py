import tempfile
from pathlib import Path

import pytest
from django.test import TestCase

from teatree.core.sealed_store import DbStore
from teatree.utils.master_key import KeyFileSource
from teatree.utils.secrets import (
    SecretNotFoundError,
    SecretStoreError,
    pass_entry_names,
    read_pass,
    read_pass_required,
    remove_pass,
    use_store,
    write_pass,
)
from tests.secret_memory_store import MemoryStore


class TestSecretStoreContract(TestCase):
    def test_facade_contract_for_memory_and_database_stores(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            stores = (MemoryStore(), DbStore(KeyFileSource(Path(temporary) / "master.key")))
            for store in stores:
                with self.subTest(store=type(store).__name__), use_store(store):
                    assert read_pass("contract/token") == ""
                    with pytest.raises(SecretNotFoundError):
                        read_pass_required("contract/token")
                    assert write_pass("contract/token", "pässwörd\nmetadata") is True
                    assert read_pass_required("contract/token") == "pässwörd"
                    assert pass_entry_names("contract") == frozenset({"token"})
                    assert remove_pass("contract/token") is True
                    assert remove_pass("contract/token") is False

    def test_database_read_failure_never_becomes_absence(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            source = KeyFileSource(Path(temporary) / "master.key")
            with use_store(DbStore(source)):
                write_pass("contract/token", "canary")
                source.path.unlink()
                with pytest.raises(SecretStoreError):
                    read_pass("contract/token")
