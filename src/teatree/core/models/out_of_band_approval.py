"""Attributable single-use override for the phase-coverage gate (#3762).

The phase-coverage gate (:mod:`teatree.core.gates.phase_coverage_gate`) refuses a
``merge_safe`` verdict for a ticket whose lifecycle ledger shows the work entered
only at ``reviewing`` — implementation done out of band, teatree handed a
finished PR. Some out-of-band work is legitimate (a docs typo, a revert, a
dependency bump), so the gate needs an escape — but an *unattributed* bypass is
precisely the hole being closed, so the escape mirrors the already-validated
``E2EBypassApproval`` (#1967) / ``OnBehalfApproval`` (#960) / ``MergeClear``
(§17.4) safety shape:

* the guarded factory :meth:`OutOfBandWorkApproval.record` is the only writer;
* a maker/coding-agent/loop ``approver_id`` is refused (``is_non_reviewer_role``),
  so the agent that skipped the lifecycle can never authorise its own skip;
* ``consumed_at`` makes the override single-use, per-ticket-per-tree;
* the scope is ``ticket`` + ``head_sha``, so an override can never carry to a
  later commit;
* ``reason`` is MANDATORY — "why was this work legitimately out of band" is the
  whole record, and a blank one is refused;
* :class:`OutOfBandWorkAudit` records who approved what, when it was used.

The user records one with ``t3 <overlay> lifecycle approve-out-of-band <id>
--approver <user-id> --head-sha <sha> --reason "<why>"``.
"""

from typing import ClassVar

from django.db import models, transaction
from django.utils import timezone

from teatree.core.models.merge_clear import is_commit_sha, is_non_reviewer_role
from teatree.core.models.ticket import Ticket


class OutOfBandWorkApprovalError(ValueError):
    """An ``OutOfBandWorkApproval`` was rejected at record time — the contract failed."""


def _canonical_sha(head_sha: str) -> str:
    return head_sha.strip().lower()


class OutOfBandWorkApproval(models.Model):
    """One recorded human authorisation to clear a ticket whose work skipped the lifecycle."""

    ticket = models.ForeignKey(Ticket, on_delete=models.CASCADE, related_name="out_of_band_approvals")
    head_sha = models.CharField(max_length=64)
    approver_id = models.CharField(max_length=255)
    reason = models.TextField()
    created_at = models.DateTimeField(default=timezone.now)
    consumed_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        db_table = "teatree_out_of_band_approval"
        ordering: ClassVar = ["-created_at"]

    def __str__(self) -> str:
        return f"out-of-band<ticket={self.ticket_id}@{self.head_sha[:8]} by {self.approver_id}>"  # ty: ignore[unresolved-attribute]

    @classmethod
    def record(cls, *, ticket: Ticket, head_sha: str, approver_id: str, reason: str) -> "OutOfBandWorkApproval":
        """The single guarded factory — validate the contract, then write one row.

        Raises :class:`OutOfBandWorkApprovalError` with a precise reason on the
        first violation: a full 40-char hex ``head_sha`` (an override binds to
        the exact reviewed tree); a non-empty ``approver_id`` that is NOT a
        maker/coding-agent/loop role; a non-empty ``reason``.
        """
        clean_sha = _canonical_sha(head_sha)
        if not is_commit_sha(clean_sha):
            msg = (
                f"head_sha {head_sha!r} is not a full 40-char hex commit SHA — an out-of-band "
                f"override binds to the exact reviewed tree (mirrors MergeClear §17.4.2). Pass the "
                f"full SHA, e.g. `git rev-parse HEAD`"
            )
            raise OutOfBandWorkApprovalError(msg)

        approver = approver_id.strip()
        if not approver:
            msg = "approver_id is required and must be non-empty — an unattributed bypass is the hole being closed"
            raise OutOfBandWorkApprovalError(msg)
        if is_non_reviewer_role(approver):
            msg = (
                f"approver_id {approver!r} is a maker/coding-agent/loop role — an out-of-band override "
                f"must be recorded by the human user, never self-authorized by the agent whose work "
                f"skipped the lifecycle (§17.8 clause 3; mirrors E2EBypassApproval #1967)"
            )
            raise OutOfBandWorkApprovalError(msg)

        explanation = reason.strip()
        if not explanation:
            msg = (
                "reason is required and must be non-empty — the override records WHY this work was "
                "legitimately done out of band (a docs typo, a revert, a dependency bump)"
            )
            raise OutOfBandWorkApprovalError(msg)

        with transaction.atomic():
            return cls.objects.create(ticket=ticket, head_sha=clean_sha, approver_id=approver, reason=explanation)

    @classmethod
    def has_unconsumed(cls, ticket: Ticket, head_sha: str) -> bool:
        """True iff an unconsumed override exists for exactly *ticket* + *head_sha* (non-claiming)."""
        return cls.objects.filter(ticket=ticket, head_sha=_canonical_sha(head_sha), consumed_at__isnull=True).exists()

    @classmethod
    def consume(cls, ticket: Ticket, head_sha: str) -> "OutOfBandWorkApproval | None":
        """Atomically claim the matching unconsumed override, or return ``None``.

        The ``consumed_at`` stamp under ``select_for_update`` keeps the claim
        single-use even under a concurrent second gate evaluation on the same
        ticket + tree.
        """
        clean_sha = _canonical_sha(head_sha)
        with transaction.atomic():
            row = (
                cls.objects.select_for_update()
                .filter(ticket=ticket, head_sha=clean_sha, consumed_at__isnull=True)
                .order_by("created_at")
                .first()
            )
            if row is None:
                return None
            row.consumed_at = timezone.now()
            row.save(update_fields=["consumed_at"])
            return row


class OutOfBandWorkAudit(models.Model):
    """Post-override audit: who approved, which ticket, which tree, why, when it was used."""

    approval = models.ForeignKey(OutOfBandWorkApproval, on_delete=models.CASCADE, related_name="audits")
    ticket = models.ForeignKey(Ticket, on_delete=models.CASCADE, related_name="out_of_band_audits")
    head_sha = models.CharField(max_length=64)
    approver_id = models.CharField(max_length=255)
    reason = models.TextField()
    executed_at = models.DateTimeField(default=timezone.now)

    class Meta:
        db_table = "teatree_out_of_band_audit"
        ordering: ClassVar = ["-executed_at"]

    def __str__(self) -> str:
        return f"out-of-band-audit<ticket={self.ticket_id}@{self.head_sha[:8]} by {self.approver_id}>"  # ty: ignore[unresolved-attribute]
