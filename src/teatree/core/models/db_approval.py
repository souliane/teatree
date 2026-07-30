"""Recorded per-invocation user-approval channel for the #777 gate (#953).

The #777 interactive-approval gate (``teatree.utils.approval``) hard-rejects
any non-TTY caller. Its only sanctioned satisfier was a human typing ``yes``
at a real terminal — so a chat-only operator plus any agent could never run a
``db refresh --fresh-dump``, stalling the whole dump/restore lifecycle on a
manual human-at-a-terminal action.

This is the database-lifecycle analogue of the already-rejected
"human-merge-only" anti-pattern; the merge keystone (``MergeClear`` /
``MergeAudit``, BLUEPRINT §17.4) solved exactly the same shape with a
recorded, durable, single-use human approval. ``DbApproval`` mirrors that
safety model 1:1 for the DB gate:

* guarded factory ``DbApproval.record`` (≈ ``MergeClear.issue()``) is the
    only way a row is written;
* an executing-agent/loop/coding-agent approver id is refused (≈ the
    maker≠checker ``is_non_reviewer_role()`` guard) — never self-authorize;
* ``consumed_at`` makes every approval single-use, per-invocation — no
    standing approval survives one op;
* ``op`` + ``tenant`` strictly scope the approval — it authorizes that one
    op+tenant and nothing else;
* ``DbAudit`` (≈ ``MergeAudit``) is the post-execution audit row: who
    approved, which op, which tenant, when.

The interactive-TTY path is unchanged and stays valid as the second
sanctioned channel of the *same* gate — this is not a new bypass, it is a
recorded second form of the one #777 approval.
"""

from typing import ClassVar

from django.db import models, transaction
from django.utils import timezone

from teatree.core.models.merge_clear import is_non_reviewer_role


def canonical_db_scope(op: str, tenant: str) -> tuple[str, str]:
    """Return the ``(op, tenant)`` scope key normalized identically for record + consume.

    Same drift class as the on-behalf target (#126): a strict exact-string
    match silently over-denied a legitimately-recorded approval whose op
    differed from the consume token only by surrounding whitespace or case.
    The op vocabulary is a small fixed set (``fresh-dump``, ``dslr-snapshot``),
    so it is whitespace-stripped AND casefolded; the tenant is a database
    identifier whose case is significant, so it is whitespace-stripped only.
    Applied at :meth:`DbApproval.record`, :meth:`DbApproval.matches`, and
    :meth:`DbApproval.consume` so the two ends can never drift.
    """
    return op.strip().casefold(), tenant.strip()


class DbApprovalError(ValueError):
    """A ``DbApproval`` was rejected at record time — the #953 contract failed."""


class DbApproval(models.Model):
    """One recorded user authorisation for exactly one op+tenant DB invocation.

    Mirrors ``MergeClear`` (BLUEPRINT §17.4.2): a durable row, single-use
    (``consumed_at``), strictly scoped (``op`` + ``tenant``), and only
    creatable through the guarded :meth:`record` factory which refuses a
    self/agent/loop approver. A consumed or scope-mismatched row is treated
    as absent by :meth:`matches`.
    """

    op = models.CharField(max_length=64)
    tenant = models.CharField(max_length=255)
    approver_id = models.CharField(max_length=255)
    created_at = models.DateTimeField(default=timezone.now)
    consumed_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        db_table = "teatree_db_approval"
        ordering: ClassVar = ["-created_at"]

    def __str__(self) -> str:
        return f"db-approval<{self.op}:{self.tenant} by {self.approver_id}>"

    @classmethod
    def record(cls, op: str, tenant: str, approver_id: str) -> "DbApproval":
        """The single guarded factory for a recorded per-invocation approval.

        Enforces the contract before any row is written and raises
        :class:`DbApprovalError` with a precise reason on the first
        violation: non-empty ``op``/``tenant``/``approver_id``; an
        ``approver_id`` that is NOT a maker/coding-agent/loop role (the
        executing agent can never self-authorize — ≈ the maker≠checker
        ``is_non_reviewer_role()`` guard on ``MergeClear.issue``).
        Construction is atomic so a rejected approval leaves no partial row.
        """
        clean_op, clean_tenant = canonical_db_scope(op, tenant)
        if not clean_op:
            msg = "op is required and must be non-empty (#953)"
            raise DbApprovalError(msg)

        if not clean_tenant:
            msg = "tenant is required and must be non-empty (#953)"
            raise DbApprovalError(msg)

        approver = approver_id.strip()
        if not approver:
            msg = "approver_id is required and must be non-empty (#953)"
            raise DbApprovalError(msg)
        if is_non_reviewer_role(approver):
            msg = (
                f"approver_id {approver!r} is a maker/coding-agent/loop role — a DbApproval "
                f"must be recorded by a user, never self-authorized by the executing agent "
                f"(#953, mirrors MergeClear §17.8 clause 3)"
            )
            raise DbApprovalError(msg)

        with transaction.atomic():
            return cls.objects.create(op=clean_op, tenant=clean_tenant, approver_id=approver)

    def matches(self, op: str, tenant: str, approver_id: str) -> bool:
        """True iff this row is unconsumed and scoped to exactly *op* + *tenant* + *approver_id*.

        A consumed approval is single-use and no longer matches (reusing it
        would let a replay slip a second unapproved op through). The scope
        is exact: an approval for ``tenant-b``+``fresh-dump`` never
        satisfies any other op or tenant, and the PRESENTED approver must
        equal the recorded one — a non-empty ``--user-authorized <token>``
        that is NOT the recorded approver can no longer consume it.
        """
        if self.consumed_at is not None:
            return False
        norm_op, norm_tenant = canonical_db_scope(op, tenant)
        return self.op == norm_op and self.tenant == norm_tenant and self.approver_id == approver_id.strip()

    @classmethod
    def consume(cls, op: str, tenant: str, approver_id: str) -> "DbApproval | None":
        """Atomically claim and consume the matching unconsumed approval, if any.

        Returns the consumed row (so the caller can write the audit) or
        ``None`` when no valid recorded approval exists for this exact
        op+tenant+approver — the caller then falls back to the interactive-TTY
        path. The claim filters on ``approver_id`` so a non-empty
        ``--user-authorized <token>`` that is NOT the recorded approver never
        consumes the approval (an empty presented approver matches nothing).
        The ``consumed_at`` stamp + ``select_for_update`` make the claim
        single-use even under a concurrent second invocation.
        """
        presented = approver_id.strip()
        if not presented:
            return None
        clean_op, clean_tenant = canonical_db_scope(op, tenant)
        with transaction.atomic():
            row = (
                cls.objects.select_for_update()
                .filter(op=clean_op, tenant=clean_tenant, approver_id=presented, consumed_at__isnull=True)
                .order_by("created_at")
                .first()
            )
            if row is None:
                return None
            row.consumed_at = timezone.now()
            row.save(update_fields=["consumed_at"])
            return row


class DbAudit(models.Model):
    """Post-execution audit of a recorded-approval DB op — ≈ ``MergeAudit`` (#953)."""

    approval = models.ForeignKey(
        DbApproval,
        on_delete=models.CASCADE,
        related_name="audits",
    )
    op = models.CharField(max_length=64)
    tenant = models.CharField(max_length=255)
    approver_id = models.CharField(max_length=255)
    executed_at = models.DateTimeField(default=timezone.now)

    class Meta:
        db_table = "teatree_db_audit"
        ordering: ClassVar = ["-executed_at"]

    def __str__(self) -> str:
        return f"db-audit<{self.op}:{self.tenant} by {self.approver_id}>"
