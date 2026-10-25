"""Database adapter for the secret facade."""

from typing import override

from django.db import OperationalError

from teatree.config.credential_pass_key import validate_pass_key_entry
from teatree.core.models.sealed_secret import SEALED_MIGRATION, SealedSecret
from teatree.utils.master_key import KeyFileSource, master_key_source
from teatree.utils.secrets import SecretStore, SecretStoreError


class DbStore(SecretStore):
    def __init__(self, source: KeyFileSource | None = None) -> None:
        self.source = source or master_key_source()

    def read(self, key: str) -> str | None:
        try:
            row = SealedSecret.objects.filter(name=key).first()
        except OperationalError as exc:
            if SealedSecret.objects.table_missing():
                return None
            message = f"sealed secret {key!r} could not be read"
            raise SecretStoreError(message) from exc
        return row.reveal(self.source.load()) if row is not None else None

    def write(self, key: str, value: str) -> bool:
        validate_pass_key_entry(key)
        try:
            has_rows = SealedSecret.objects.exists()
        except OperationalError as exc:
            if SealedSecret.objects.table_missing():
                message = f"sealed secret write requires migration {SEALED_MIGRATION} on the canonical database"
                raise SecretStoreError(message) from exc
            message = f"sealed secret {key!r} could not be written"
            raise SecretStoreError(message) from exc
        active = self.source.load() if has_rows else self.source.create()
        SealedSecret.objects.store(key, value, active)
        return True

    @override
    def remove(self, key: str) -> bool:
        try:
            return SealedSecret.objects.discard(key)
        except OperationalError as exc:
            if SealedSecret.objects.table_missing():
                return False
            message = f"sealed secret {key!r} could not be removed"
            raise SecretStoreError(message) from exc

    @override
    def names(self, prefix: str) -> frozenset[str]:
        try:
            return SealedSecret.objects.names(prefix)
        except OperationalError as exc:
            if SealedSecret.objects.table_missing():
                return frozenset()
            message = f"sealed secret names under {prefix!r} could not be read"
            raise SecretStoreError(message) from exc
