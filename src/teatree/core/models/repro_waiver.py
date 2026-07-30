"""Human-authorized waiver of the forced-repro gate for a genuinely repro-less fix (#118).

Some failures cannot be determinized — a race, a heisenbug, a hardware-timing
bug. For those the forced-repro gate must be satisfiable without an executed
RED->GREEN pair, but the escape must be **audited**, not a silent skip.

``ReproWaiver`` is HUMAN-AUTHORIZED (maker != checker, mirroring the
``plan-bypass --human-authorize`` pattern and ``E2EBypassApproval``'s guard),
deliberately stronger than ``fix_record_dod``'s self-authored
``fix_record_override``. The asymmetry is intentional: #118's whole premise is
that the agent's self-judgment about "this fix is right" is unreliable without an
executed repro, so a self-waivable repro gate would defeat itself on every fix.
The guarded :meth:`record` factory refuses a maker/coding-agent/loop
``approver_id`` (the executing agent can never waive its own repro discipline)
and refuses an empty reason. The waiver is a standing per-ticket property — repro
discipline is per-ticket — so it is not single-use-consumed.
"""

from typing import ClassVar

from django.db import models, transaction
from django.utils import timezone

from teatree.core.models.merge_clear import is_non_reviewer_role
from teatree.core.models.ticket import Ticket


class ReproWaiverError(ValueError):
    """A ``ReproWaiver`` was rejected at record time — the maker != checker contract failed."""


class ReproWaiver(models.Model):
    """One human authorisation to ship a repro-less FIX ticket without executed evidence (#118)."""

    ticket = models.ForeignKey(
        Ticket,
        on_delete=models.CASCADE,
        related_name="repro_waivers",
    )
    approver_id = models.CharField(max_length=255)
    reason = models.TextField()
    created_at = models.DateTimeField(default=timezone.now)

    class Meta:
        db_table = "teatree_repro_waiver"
        ordering: ClassVar = ["-created_at"]

    def __str__(self) -> str:
        return f"repro-waiver<ticket={self.ticket_id} by {self.approver_id}>"  # ty: ignore[unresolved-attribute]

    @classmethod
    def record(cls, *, ticket: Ticket, approver_id: str, reason: str) -> "ReproWaiver":
        """The single guarded factory for a human-authorized repro waiver.

        Refuses an empty *reason* (the waiver must state why this failure class
        is genuinely repro-less) and refuses a maker/coding-agent/loop
        *approver_id* — the executing agent can never self-authorize the waiver
        of the very discipline that exists because its self-judgment is
        unreliable (the maker != checker guard shared with ``E2EBypassApproval``
        / ``MergeClear`` §17.8).
        """
        clean_reason = reason.strip()
        if not clean_reason:
            msg = "reason is required and must be non-empty — state why this failure class is genuinely repro-less"
            raise ReproWaiverError(msg)
        approver = approver_id.strip()
        if not approver:
            msg = "approver_id is required and must be non-empty (#118)"
            raise ReproWaiverError(msg)
        if is_non_reviewer_role(approver):
            msg = (
                f"approver_id {approver!r} is a maker/coding-agent/loop role — a repro waiver must be recorded "
                f"by the human user, never self-authorized by the executing agent (#118, mirrors E2EBypassApproval "
                f"#1967 / MergeClear §17.8). A self-waivable repro gate would defeat itself on every fix"
            )
            raise ReproWaiverError(msg)
        with transaction.atomic():
            return cls.objects.create(ticket=ticket, approver_id=approver, reason=clean_reason)
