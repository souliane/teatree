import hashlib
import json
from dataclasses import dataclass
from datetime import timedelta
from typing import ClassVar

from django.db import IntegrityError, models, transaction
from django.utils import timezone

from teatree.core.models.reviewer_identity import is_non_reviewer_role
from teatree.hooks.gate_escape import EscapePolicy, policy_for


class GateEscapeError(ValueError):
    pass


@dataclass(frozen=True, slots=True)
class EscapeScope:
    gate: str
    reason: str
    actor: str
    subject: str
    head: str
    action_digest: str
    session_id: str
    call_id: str

    def binding(self) -> str:
        parts = (self.gate, self.actor, self.subject, self.head, self.action_digest, self.session_id)
        return hashlib.sha256(json.dumps(parts, separators=(",", ":")).encode()).hexdigest()


class GateEscape(models.Model):
    class Issuer(models.TextChoices):
        AGENT = "agent", "Agent"
        HUMAN = "human", "Human"

    gate = models.CharField(max_length=80)
    reason = models.TextField()
    actor = models.CharField(max_length=255)
    issuer_kind = models.CharField(max_length=8, choices=Issuer.choices)
    issuer_id = models.CharField(max_length=255)
    subject = models.CharField(max_length=1024)
    head = models.CharField(max_length=64, blank=True)
    action_digest = models.CharField(max_length=64)
    session_id = models.CharField(max_length=255)
    call_id = models.CharField(max_length=255, blank=True)
    issued_at = models.DateTimeField(default=timezone.now)
    expires_at = models.DateTimeField()
    recorded_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        db_table = "teatree_gate_escape"
        constraints: ClassVar = [
            models.UniqueConstraint(
                fields=("gate", "actor", "subject", "head", "action_digest", "session_id", "call_id"),
                name="uniq_gate_escape_action",
            )
        ]

    def __str__(self) -> str:
        return f"gate-escape<{self.gate}:{self.session_id}>"

    @classmethod
    def _scope_fields(cls, scope: EscapeScope) -> dict[str, str]:
        if not all(
            (scope.gate, scope.reason.strip(), scope.actor, scope.subject, scope.action_digest, scope.session_id)
        ):
            message = "Gate escape scope is incomplete"
            raise GateEscapeError(message)
        return {
            "gate": scope.gate,
            "actor": scope.actor,
            "subject": scope.subject,
            "head": scope.head,
            "action_digest": scope.action_digest,
            "session_id": scope.session_id,
        }

    @classmethod
    def record_agent(cls, scope: EscapeScope) -> "GateEscape":
        if policy_for(scope.gate) is not EscapePolicy.AUDITED:
            message = "This gate does not accept agent escapes"
            raise GateEscapeError(message)
        fields = cls._scope_fields(scope)
        now = timezone.now()
        try:
            with transaction.atomic():
                return cls.objects.create(
                    **fields,
                    reason=scope.reason.strip(),
                    issuer_kind=cls.Issuer.AGENT,
                    issuer_id=scope.actor,
                    call_id=scope.call_id,
                    issued_at=now,
                    expires_at=now + timedelta(minutes=5),
                    recorded_at=now,
                )
        except IntegrityError as exc:
            message = "Gate escape was already used for this action"
            raise GateEscapeError(message) from exc

    @classmethod
    def grant_human(cls, scope: EscapeScope, *, issuer_id: str, ttl: timedelta) -> "GateEscape":
        if policy_for(scope.gate) is not EscapePolicy.HUMAN_ONLY:
            message = "This gate does not accept human grants"
            raise GateEscapeError(message)
        if not issuer_id.strip() or is_non_reviewer_role(issuer_id) or issuer_id == scope.actor:
            message = "Gate escape issuer must be the independent owner"
            raise GateEscapeError(message)
        if ttl <= timedelta(0):
            message = "Gate escape expiry must be in the future"
            raise GateEscapeError(message)
        fields = cls._scope_fields(scope)
        now = timezone.now()
        try:
            with transaction.atomic():
                return cls.objects.create(
                    **fields,
                    reason=scope.reason.strip(),
                    issuer_kind=cls.Issuer.HUMAN,
                    issuer_id=issuer_id,
                    call_id="",
                    issued_at=now,
                    expires_at=now + ttl,
                )
        except IntegrityError as exc:
            message = "Gate escape grant already exists for this action"
            raise GateEscapeError(message) from exc

    @classmethod
    def consume_human(cls, scope: EscapeScope) -> "GateEscape | None":
        fields = cls._scope_fields(scope)
        now = timezone.now()
        with transaction.atomic():
            row = (
                cls.objects.filter(
                    **fields,
                    issuer_kind=cls.Issuer.HUMAN,
                    issued_at__lte=now,
                    expires_at__gt=now,
                    recorded_at__isnull=True,
                )
                .select_for_update()
                .first()
            )
            if row is None:
                return None
            row.call_id = scope.call_id
            row.recorded_at = now
            row.save(update_fields=["call_id", "recorded_at"])
            return row
