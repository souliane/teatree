"""Durable per-diff merge authorisation + post-merge audit (BLUEPRINT §17.4).

``MergeClear`` is the orchestrator-issued, compaction-surviving record that
authorises execution of exactly one merge (§17.4.2). It is a dedicated Django
row alongside ``Ticket``/``Session``/``Task`` — explicitly NOT a
session-volatile JSON file: the orchestrator that issues it may be compacted
or restarted before the durable loop acts on it, so the canonical tier is the
DB. The loop re-reads the row at merge time and never trusts an in-memory copy
carried across the orchestrator → loop handoff.

``MergeAudit`` is the loop's independent post-merge signal back into the
flywheel (§17.4.4): one row per executed merge, written to the same canonical
tier so it survives the orchestrator's compaction/restart by construction.

Both rows are written through the same ``transaction.atomic()`` path that gets
``BEGIN IMMEDIATE`` write-serialization on the production SQLite engine (§4.3).
"""

from typing import ClassVar

from django.db import models
from django.utils import timezone

from teatree.core.models.ticket import Ticket


class MergeClear(models.Model):
    """One orchestrator-issued authorisation for exactly one PR merge (§17.4.2).

    No partial CLEAR is actionable: a row missing any load-bearing field is
    treated as absent by :meth:`is_actionable`. ``reviewed_sha`` binds the
    authorisation to the exact tree the orchestrator reviewed — the loop
    refuses to merge if GitHub's live head moved off it (§17.4.3, the
    TOCTOU/replay defence closed by ``expected_head_oid``).
    """

    class BlastClass(models.TextChoices):
        SUBSTRATE = "substrate", "Substrate (healing/gate substrate)"
        LOGIC = "logic", "Logic (non-substrate business logic)"
        DOCS = "docs", "Docs (documentation/spec only)"

    class VerifyResult(models.TextChoices):
        GREEN = "green", "Green"
        PENDING = "pending", "Pending"
        FAILED = "failed", "Failed"

    ticket = models.ForeignKey(
        Ticket,
        on_delete=models.CASCADE,
        related_name="merge_clears",
        null=True,
        blank=True,
    )
    pr_id = models.IntegerField()
    slug = models.CharField(max_length=255)
    reviewed_sha = models.CharField(max_length=64)
    reviewer_identity = models.CharField(max_length=255)
    gh_verify_result = models.CharField(max_length=32, choices=VerifyResult.choices)
    blast_class = models.CharField(max_length=16, choices=BlastClass.choices)
    issued_at = models.DateTimeField(default=timezone.now)
    consumed_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        db_table = "teatree_merge_clear"
        ordering: ClassVar = ["-issued_at"]

    def __str__(self) -> str:
        return f"merge-clear<{self.slug}#{self.pr_id}@{self.reviewed_sha[:8]}>"

    def is_actionable(self) -> bool:
        """True iff every load-bearing field is populated and the CLEAR is unconsumed.

        §17.4.2: "A ``MergeClear`` row missing any field is treated as
        absent." A consumed CLEAR (already used for a successful merge)
        is single-use and no longer actionable — reusing it would let a
        replay slip a second, unreviewed merge through.
        """
        if self.consumed_at is not None:
            return False
        required = (
            self.pr_id,
            self.slug,
            self.reviewed_sha,
            self.reviewer_identity,
            self.gh_verify_result,
            self.blast_class,
        )
        return all(bool(value) for value in required)


class MergeAudit(models.Model):
    """Post-merge audit record — the loop's independent flywheel signal (§17.4.4)."""

    clear = models.ForeignKey(
        MergeClear,
        on_delete=models.CASCADE,
        related_name="audits",
    )
    merged_sha = models.CharField(max_length=64)
    merged_at = models.DateTimeField(default=timezone.now)
    required_checks_status = models.CharField(max_length=32)

    class Meta:
        db_table = "teatree_merge_audit"
        ordering: ClassVar = ["-merged_at"]

    def __str__(self) -> str:
        return f"merge-audit<{self.clear.slug}#{self.clear.pr_id}@{self.merged_sha[:8]}>"
