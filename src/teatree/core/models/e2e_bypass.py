"""Single-use user-approval channel for the mandatory-E2E gate bypass (#1967).

The mandatory-E2E gate (``teatree.core.gates.e2e_mandatory_gate``) refuses to ship or
CLEAR a customer-display-impacting change without recorded green E2E evidence.
Its only bypass must require **explicit user approval** — never the implementing
agent's own judgment — so it mirrors the already-validated ``OnBehalfApproval``
(#960) / ``DbApproval`` (#953) / ``MergeClear`` (§17.4) safety shape:

* guarded factory :meth:`E2EBypassApproval.record` is the only way a row is
    written;
* a maker/coding-agent/loop approver id is refused (the ``is_non_reviewer_role``
    guard) — the executing agent can never authorize its own bypass;
* ``consumed_at`` makes every bypass single-use, per-ticket-per-tree;
* the scope is ``ticket`` + ``head_sha`` (the reviewed tree): a bypass
    authorizes shipping that one ticket at that one SHA and nothing else, so it
    can never carry to a later commit;
* :class:`E2EBypassAudit` is the post-bypass audit row (who approved, which
    ticket, which SHA, when).

The user records a bypass with ``t3 <overlay> ticket e2e-bypass <id> --approver
<user-id>``; the next gate evaluation at the same head SHA consumes it and the
gate passes once.
"""

from typing import ClassVar

from django.db import models, transaction
from django.utils import timezone

from teatree.core.models.merge_clear import is_commit_sha, is_non_reviewer_role
from teatree.core.models.ticket import Ticket


class E2EBypassApprovalError(ValueError):
    """An ``E2EBypassApproval`` was rejected at record time — the contract failed."""


def _canonical_sha(head_sha: str) -> str:
    return head_sha.strip().lower()


class E2EBypassApproval(models.Model):
    """One recorded user authorisation to ship a ticket at a tree without E2E.

    Mirrors ``OnBehalfApproval`` (#960) / ``MergeClear`` (§17.4.2): a durable
    row, single-use (``consumed_at``), strictly scoped (``ticket`` +
    ``head_sha``), creatable only through the guarded :meth:`record` factory
    which refuses a maker/coding-agent/loop approver.
    """

    ticket = models.ForeignKey(
        Ticket,
        on_delete=models.CASCADE,
        related_name="e2e_bypass_approvals",
    )
    head_sha = models.CharField(max_length=64)
    approver_id = models.CharField(max_length=255)
    created_at = models.DateTimeField(default=timezone.now)
    consumed_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        db_table = "teatree_e2e_bypass_approval"
        ordering: ClassVar = ["-created_at"]

    def __str__(self) -> str:
        return f"e2e-bypass<ticket={self.ticket_id}@{self.head_sha[:8]} by {self.approver_id}>"  # ty: ignore[unresolved-attribute]

    @classmethod
    def record(cls, *, ticket: Ticket, head_sha: str, approver_id: str) -> "E2EBypassApproval":
        """The single guarded factory for a recorded per-tree E2E bypass.

        Enforces the contract before any row is written and raises
        :class:`E2EBypassApprovalError` with a precise reason on the first
        violation: a full 40-char hex ``head_sha`` (a bypass binds to the
        exact reviewed tree); a non-empty ``approver_id`` that is NOT a
        maker/coding-agent/loop role (the executing agent can never authorize
        its own bypass — the maker≠checker guard shared with ``MergeClear``).
        Construction is atomic so a rejected approval leaves no partial row.
        """
        clean_sha = _canonical_sha(head_sha)
        if not is_commit_sha(clean_sha):
            msg = (
                f"head_sha {head_sha!r} is not a full 40-char hex commit SHA — an E2E bypass "
                f"binds to the exact reviewed tree (#1967, mirrors MergeClear §17.4.2). Pass the "
                f"full SHA, e.g. `git rev-parse HEAD`"
            )
            raise E2EBypassApprovalError(msg)

        approver = approver_id.strip()
        if not approver:
            msg = "approver_id is required and must be non-empty (#1967)"
            raise E2EBypassApprovalError(msg)
        if is_non_reviewer_role(approver):
            msg = (
                f"approver_id {approver!r} is a maker/coding-agent/loop role — an E2E bypass "
                f"must be recorded by the human user, never self-authorized by the executing "
                f"agent (#1967, mirrors OnBehalfApproval #960 / DbApproval #953 / MergeClear §17.8)"
            )
            raise E2EBypassApprovalError(msg)

        with transaction.atomic():
            return cls.objects.create(ticket=ticket, head_sha=clean_sha, approver_id=approver)

    @classmethod
    def has_unconsumed(cls, ticket: Ticket, head_sha: str) -> bool:
        """True iff an unconsumed bypass is recorded for exactly *ticket* + *head_sha*.

        The read-only peek behind the gate's early refusal: it reports whether
        a consume *would* succeed without claiming the row. It never stamps
        ``consumed_at`` — the single-use claim stays inside :meth:`consume`,
        run atomically with the gate's pass decision.
        """
        return cls.objects.filter(ticket=ticket, head_sha=_canonical_sha(head_sha), consumed_at__isnull=True).exists()

    @classmethod
    def consume(cls, ticket: Ticket, head_sha: str) -> "E2EBypassApproval | None":
        """Atomically claim and consume the matching unconsumed bypass, if any.

        Returns the consumed row (so the caller can write the audit) or
        ``None`` when no valid recorded bypass exists for this exact
        ticket+SHA. The ``consumed_at`` stamp + ``select_for_update`` make the
        claim single-use even under a concurrent second gate evaluation on the
        same ticket+SHA.
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


class E2EBypassAudit(models.Model):
    """Post-bypass audit of a recorded-approval E2E bypass (#1967).

    ≈ ``OnBehalfAudit`` (#960) / ``MergeAudit`` (§17.4): who approved, which
    ticket, which tree, when the bypass was actually used.
    """

    approval = models.ForeignKey(
        E2EBypassApproval,
        on_delete=models.CASCADE,
        related_name="audits",
    )
    ticket = models.ForeignKey(
        Ticket,
        on_delete=models.CASCADE,
        related_name="e2e_bypass_audits",
    )
    head_sha = models.CharField(max_length=64)
    approver_id = models.CharField(max_length=255)
    executed_at = models.DateTimeField(default=timezone.now)

    class Meta:
        db_table = "teatree_e2e_bypass_audit"
        ordering: ClassVar = ["-executed_at"]

    def __str__(self) -> str:
        return f"e2e-bypass-audit<ticket={self.ticket_id}@{self.head_sha[:8]} by {self.approver_id}>"  # ty: ignore[unresolved-attribute]
