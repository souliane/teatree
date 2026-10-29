from collections.abc import Iterable
from typing import ClassVar, override

from django.conf import settings
from django.db import models
from django.utils import timezone

_PLATFORMS = frozenset({"github", "gitlab", "slack"})


def identity_key(platform: str, handle: str) -> str:
    platform = platform.strip().casefold()
    handle = handle.strip()
    if platform not in _PLATFORMS or not handle or ":" in handle:
        msg = "operator identity requires a supported platform and handle"
        raise ValueError(msg)
    return f"{platform}:{handle if platform == 'slack' else handle.casefold()}"


def canonical_operator_key(value: str) -> str:
    platform, separator, handle = value.partition(":")
    if not separator:
        msg = "operator identity requires platform:handle"
        raise ValueError(msg)
    return identity_key(platform, handle)


class OperatorIdentity(models.Model):
    user = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="operator_identities")
    platform = models.CharField(max_length=16)
    handle = models.CharField(max_length=160)
    external_id = models.CharField(max_length=160, blank=True, default="")
    created_at = models.DateTimeField(default=timezone.now)

    class Meta:
        db_table = "teatree_operator_identity"
        verbose_name_plural = "operator identities"
        constraints: ClassVar = [
            models.UniqueConstraint(fields=["platform", "handle"], name="unique_operator_platform_handle"),
            models.UniqueConstraint(
                fields=["platform", "external_id"],
                condition=~models.Q(external_id=""),
                name="unique_operator_platform_external_id",
            ),
        ]

    def __str__(self) -> str:
        return self.key

    @override
    def save(
        self,
        force_insert: bool = False,
        force_update: bool = False,
        using: str | None = None,
        update_fields: Iterable[str] | None = None,
    ) -> None:
        self.platform, self.handle = identity_key(self.platform, self.handle).split(":", 1)
        super().save(force_insert=force_insert, force_update=force_update, using=using, update_fields=update_fields)

    @property
    def key(self) -> str:
        return identity_key(self.platform, self.handle)
