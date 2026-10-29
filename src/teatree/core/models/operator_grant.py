"""Per-user forge OAuth grants sealed under the installation master key."""

import hmac
import json
from dataclasses import dataclass
from datetime import datetime
from typing import ClassVar

from django.conf import settings
from django.contrib.auth.models import AbstractBaseUser
from django.db import models, transaction
from django.utils import timezone

from teatree.core.models.on_behalf_approval import OnBehalfApproval, canonical_on_behalf_target
from teatree.core.models.operator_identity import OperatorIdentity, canonical_operator_key
from teatree.utils.sealing import MasterKey, SealedValue, aad

_FORGES = frozenset({"github", "gitlab"})


@dataclass(frozen=True, slots=True)
class OperatorGrantTokens:
    access_token: str
    refresh_token: str
    expires_at: datetime
    refresh_expires_at: datetime | None = None
    scope: str = ""


class OperatorGrantReadBackError(ValueError):
    def __init__(self) -> None:
        super().__init__("operator OAuth grant failed read-back")


def _grant_aad(user_id: object, forge: str) -> bytes:
    return aad("user", f"{user_id}:oauth:{forge}")


class OperatorGrantManager(models.Manager["OperatorGrant"]):
    def store(
        self,
        *,
        user: AbstractBaseUser,
        forge: str,
        tokens: OperatorGrantTokens,
        key: MasterKey,
    ) -> "OperatorGrant":
        provider = forge.strip().casefold()
        if provider not in _FORGES or not user.pk or not tokens.access_token or not tokens.refresh_token:
            message = "a grant requires a user, supported forge, access token, and refresh token"
            raise ValueError(message)
        payload = json.dumps({"access": tokens.access_token, "refresh": tokens.refresh_token}, separators=(",", ":"))
        sealed = key.seal(payload, aad=_grant_aad(user.pk, provider))
        with transaction.atomic(using=self.db):
            grant, _ = self.update_or_create(
                user=user,
                forge=provider,
                defaults={
                    "key_id": sealed.key_id,
                    "nonce": sealed.nonce,
                    "ciphertext": sealed.ciphertext,
                    "expires_at": tokens.expires_at,
                    "refresh_expires_at": tokens.refresh_expires_at,
                    "scope": tokens.scope,
                },
            )
            actual = grant.reveal(key)
            if not hmac.compare_digest(actual.access_token, tokens.access_token) or not hmac.compare_digest(
                actual.refresh_token, tokens.refresh_token
            ):
                raise OperatorGrantReadBackError
            return grant


class OperatorGrant(models.Model):
    user = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="oauth_grants")
    user_id: int
    forge = models.CharField(max_length=16)
    key_id = models.CharField(max_length=16)
    nonce = models.BinaryField()
    ciphertext = models.BinaryField()
    expires_at = models.DateTimeField()
    refresh_expires_at = models.DateTimeField(null=True, blank=True)
    scope = models.TextField(blank=True, default="")
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    objects: ClassVar[OperatorGrantManager] = OperatorGrantManager()

    class Meta:
        db_table = "teatree_operator_grant"
        constraints: ClassVar = [models.UniqueConstraint(fields=["user", "forge"], name="unique_operator_user_forge")]

    def __str__(self) -> str:
        return f"operator-grant<{self.user_id}:{self.forge}>"

    def reveal(self, key: MasterKey) -> OperatorGrantTokens:
        plaintext = key.open(
            SealedValue(self.key_id, bytes(self.nonce), bytes(self.ciphertext)),
            aad=_grant_aad(self.user_id, self.forge),
        )
        values = json.loads(plaintext)
        return OperatorGrantTokens(
            access_token=values["access"],
            refresh_token=values["refresh"],
            expires_at=self.expires_at,
            refresh_expires_at=self.refresh_expires_at,
            scope=self.scope,
        )

    def token_for(self, action: str, target: str, approver_id: str, *, key: MasterKey) -> str:
        if action not in {"approve", "unapprove"} or not target or self.expires_at <= timezone.now():
            message = "operator grant cannot authorize this action"
            raise PermissionError(message)
        required_scope = "api" if self.forge == "gitlab" else "repo"
        if required_scope not in self.scope.replace(",", " ").split():
            message = "operator grant lacks the approval scope"
            raise PermissionError(message)
        try:
            identity_key = canonical_operator_key(approver_id)
        except ValueError as exc:
            message = "operator grant identity is invalid"
            raise PermissionError(message) from exc
        identity = OperatorIdentity.objects.filter(
            user_id=self.user_id, platform=self.forge, user__is_active=True, user__is_superuser=True
        ).first()
        if identity is None or identity.key != identity_key:
            message = "operator grant does not belong to the approver"
            raise PermissionError(message)
        if not OnBehalfApproval.objects.filter(
            target=canonical_on_behalf_target(target),
            action=action,
            approver_id=identity_key,
            consumed_at__isnull=True,
        ).exists():
            message = "operator grant has no consent for this target"
            raise PermissionError(message)
        return self.reveal(key).access_token
