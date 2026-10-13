"""Instance credentials sealed under the installation master key."""

import hmac
from dataclasses import dataclass
from typing import ClassVar

from django.db import models, transaction
from django.db.models.functions import Length
from django.db.models.lookups import Exact, GreaterThanOrEqual

from teatree.config.credential_pass_key import validate_pass_key_entry
from teatree.utils.master_key import MasterKeySource
from teatree.utils.sealing import MasterKey, SealedValue, SealedValueTamperedError, aad


@dataclass(frozen=True, slots=True)
class KeyHealth:
    source: str
    status: str
    detail: str
    row_count: int

    @property
    def healthy(self) -> bool:
        return self.status == "OK"


class SealedStoreReadBackError(ValueError):
    def __init__(self, name: str) -> None:
        super().__init__(f"sealed secret {name!r} failed read-back")


class SealedSecretManager(models.Manager["SealedSecret"]):
    def names(self, prefix: str) -> frozenset[str]:
        root = prefix.rstrip("/") + "/"
        return frozenset(
            name[len(root) :]
            for name in self.filter(name__startswith=root).values_list("name", flat=True)
            if "/" not in name[len(root) :]
        )

    def reveal(self, name: str, key: MasterKey) -> str | None:
        row = self.filter(name=name).first()
        return row.reveal(key) if row is not None else None

    def store(self, name: str, value: str, key: MasterKey) -> None:
        validate_pass_key_entry(name)
        sealed = key.seal(value, aad=aad("instance", name))
        with transaction.atomic(using=self.db):
            self.update_or_create(
                name=name,
                defaults={"key_id": sealed.key_id, "nonce": sealed.nonce, "ciphertext": sealed.ciphertext},
            )
            read_back = self.reveal(name, key)
            if read_back is None or not hmac.compare_digest(read_back, value):
                raise SealedStoreReadBackError(name)

    def discard(self, name: str) -> bool:
        removed, _ = self.filter(name=name).delete()
        return removed > 0

    def key_census(self) -> dict[str, int]:
        return {row["key_id"]: row["count"] for row in self.values("key_id").annotate(count=models.Count("id"))}

    def key_health(self, source: MasterKeySource) -> KeyHealth:
        rows = list(self.all())
        if not rows:
            return KeyHealth(source.label, "OK", "no sealed rows", 0)
        try:
            key = source.load()
        except ValueError as exc:
            detail = str(exc)
            status = "INVALID" if "base64" in detail or "32 bytes" in detail else "UNREADABLE"
            return KeyHealth(source.label, status, detail, len(rows))
        ids = {row.key_id for row in rows}
        if ids != {key.key_id}:
            return KeyHealth(
                source.label,
                "WRONG_KEY",
                f"stored key_ids {', '.join(sorted(ids))}; active key_id {key.key_id}",
                len(rows),
            )
        try:
            for row in rows:
                row.reveal(key)
        except SealedValueTamperedError as exc:
            return KeyHealth(source.label, "TAMPERED", str(exc), len(rows))
        return KeyHealth(source.label, "OK", "all sealed rows readable", len(rows))


class SealedSecret(models.Model):
    name = models.CharField(max_length=255, unique=True)
    key_id = models.CharField(max_length=16, db_index=True)
    nonce = models.BinaryField()
    ciphertext = models.BinaryField()
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    objects: ClassVar[SealedSecretManager] = SealedSecretManager()

    class Meta:
        db_table = "teatree_sealed_secret"
        constraints: ClassVar = [
            models.CheckConstraint(condition=~models.Q(name=""), name="sealed_secret_name_required"),
            models.CheckConstraint(condition=models.Q(Exact(Length("key_id"), 16)), name="sealed_secret_key_id_length"),
            models.CheckConstraint(condition=models.Q(Exact(Length("nonce"), 12)), name="sealed_secret_nonce_length"),
            models.CheckConstraint(
                condition=models.Q(GreaterThanOrEqual(Length("ciphertext"), 16)),
                name="sealed_secret_ciphertext_length",
            ),
        ]

    def __str__(self) -> str:
        return f"sealed-secret<{self.name}>"

    def reveal(self, key: MasterKey) -> str:
        return key.open(
            SealedValue(self.key_id, bytes(self.nonce), bytes(self.ciphertext)),
            aad=aad("instance", self.name),
        )
