"""Database adapter for the secret facade."""

from typing import override

from django.db import OperationalError, connections, transaction

from teatree.config.credential_pass_key import validate_pass_key_entry
from teatree.core.models.sealed_secret import SEALED_MIGRATION, SealedSecret
from teatree.utils.master_key import KeyFileSource, master_key_source
from teatree.utils.secrets import SecretStore, SecretStoreError


class DbStore(SecretStore):
    label = "the sealed secret store"

    def __init__(self, source: KeyFileSource | None = None) -> None:
        self.source = source or master_key_source()

    @override
    def read(self, key: str) -> str | None:
        try:
            row = SealedSecret.objects.filter(name=key).first()
        except OperationalError as exc:
            if SealedSecret.objects.table_missing():
                return None
            message = f"sealed secret {key!r} could not be read"
            raise SecretStoreError(message) from exc
        return row.reveal(self.source.load()) if row is not None else None

    @override
    def write(self, key: str, value: str) -> bool:
        validate_pass_key_entry(key)
        alias = SealedSecret.objects.db
        try:
            with transaction.atomic(using=alias):
                connection = connections[alias]
                with connection.cursor() as cursor:
                    if connection.vendor == "sqlite":
                        cursor.execute("UPDATE teatree_sealed_secret SET key_id = key_id WHERE 0")
                    elif connection.vendor == "postgresql":
                        cursor.execute("SELECT pg_advisory_xact_lock(%s)", [5538])
                    else:
                        message = f"sealed secret writes require a database write lock on {connection.vendor}"
                        raise SecretStoreError(message)
                stored_ids = set(SealedSecret.objects.key_census())
                active = self.source.load() if stored_ids else self.source.create()
                if stored_ids and stored_ids != {active.key_id}:
                    message = (
                        f"refusing to write sealed secret {key!r}: stored rows are sealed under key_id "
                        f"{', '.join(sorted(stored_ids))} but the active key is {active.key_id}"
                    )
                    raise SecretStoreError(message)
                SealedSecret.objects.store(key, value, active)
        except OperationalError as exc:
            if SealedSecret.objects.table_missing():
                message = f"sealed secret write requires migration {SEALED_MIGRATION} on the canonical database"
                raise SecretStoreError(message) from exc
            message = f"sealed secret {key!r} could not be written"
            raise SecretStoreError(message) from exc
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

    @override
    def remedy(self, key: str) -> str:
        return f"`t3 teatree secrets set {key}`"
