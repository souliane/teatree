"""Database adapter for the secret facade."""

from typing import override

from teatree.core.models.sealed_secret import SealedSecret
from teatree.utils.master_key import KeyFileSource, master_key_source
from teatree.utils.secrets import SecretStore


class DbStore(SecretStore):
    def __init__(self, source: KeyFileSource | None = None) -> None:
        self.source = source or master_key_source()

    def read(self, key: str) -> str | None:
        row = SealedSecret.objects.filter(name=key).first()
        return row.reveal(self.source.load()) if row is not None else None

    def write(self, key: str, value: str) -> bool:
        active = self.source.load() if SealedSecret.objects.exists() else self.source.create()
        SealedSecret.objects.store(key, value, active)
        return True

    @override
    def remove(self, key: str) -> bool:
        return SealedSecret.objects.discard(key)

    @override
    def names(self, prefix: str) -> frozenset[str]:
        return SealedSecret.objects.names(prefix)
