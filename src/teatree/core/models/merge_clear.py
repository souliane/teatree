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

from dataclasses import dataclass
from typing import ClassVar

from django.db import models, transaction
from django.utils import timezone

from teatree.core.models.ticket import Ticket

# §17.8 clause 3 / §17.6 candidate 13: an independent cold-review attestation
# cannot be recorded by the maker/coding-agent/loop side — the author would be
# rubber-stamping their own work. The CLEAR-issuer guard (this module) and the
# `reviewing`-attestation guard (lifecycle command) share this single list so
# they cannot drift apart. It lives on the model because the model owns the
# CLEAR contract (§17.4.2); the command layer imports it from here.
NON_REVIEWER_AGENT_PREFIXES = ("maker:", "maker-", "coding-agent", "coding", "loop")

_SHA_ALPHABET = frozenset("0123456789abcdef")
_MIN_SHA_LEN = 7


def is_non_reviewer_role(identity: str) -> bool:
    """True iff ``identity`` is a maker/coding-agent/loop role (§17.8 clause 3)."""
    lowered = identity.strip().lower()
    return any(lowered == prefix or lowered.startswith(prefix) for prefix in NON_REVIEWER_AGENT_PREFIXES)


def is_commit_sha(value: str) -> bool:
    """True iff ``value`` looks like a hex commit id, not a branch ref (§17.4.2)."""
    candidate = value.strip().lower()
    return len(candidate) >= _MIN_SHA_LEN and all(char in _SHA_ALPHABET for char in candidate)


class ClearIssuanceError(ValueError):
    """A per-diff CLEAR was rejected at issue time — the §17.4.2/§17.8 contract failed."""


@dataclass(frozen=True, slots=True)
class ClearRequest:
    """The orchestrator's per-diff CLEAR inputs (BLUEPRINT §17.4.2), validated as a unit.

    A single value object so the guarded factory (:meth:`MergeClear.issue`)
    takes one argument instead of the irreducible §17.4.2 field list — the
    contract is the dataclass, not a long parameter list.
    """

    pr_id: int
    slug: str
    reviewed_sha: str
    reviewer_identity: str
    gh_verify_result: str = "green"
    blast_class: str = "logic"
    ticket: "Ticket | None" = None
    human_authorizer: str = ""
    executing_loop_identity: str = "merge-loop"


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
    # Set ONLY for a substrate-class CLEAR a human/owner explicitly authorised
    # to merge (§17.4.3 step 5 / invariant 4). Empty for every non-substrate
    # CLEAR and for an un-authorised substrate CLEAR — the loop never
    # auto-merges substrate; the human-merge path is the only way a substrate
    # CLEAR becomes mergeable, and it MUST still go through the sanctioned
    # ``t3 ... ticket merge`` transition (invariant 8), never raw ``gh``. This
    # records *who* authorised it so the FSM/audit stays coherent.
    human_authorizer = models.CharField(max_length=255, blank=True, default="")
    issued_at = models.DateTimeField(default=timezone.now)
    consumed_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        db_table = "teatree_merge_clear"
        ordering: ClassVar = ["-issued_at"]

    def __str__(self) -> str:
        return f"merge-clear<{self.slug}#{self.pr_id}@{self.reviewed_sha[:8]}>"

    @classmethod
    def issue(cls, request: ClearRequest) -> "MergeClear":
        """The single guarded factory for a per-diff CLEAR (BLUEPRINT §17.4.2 / §17.8 clause 3).

        Enforces the issuance contract before any row is written and raises
        :class:`ClearIssuanceError` with a precise reason on the first
        violation: a known ``blast_class``/``gh_verify_result``; a non-empty
        ``reviewer_identity`` that is neither the executing loop nor a
        maker/coding-agent/loop role (the author cannot self-attest); a
        ``reviewed_sha`` that is a hex commit id (not a branch ref);
        ``human_authorizer`` only on a substrate CLEAR. Construction is
        atomic so a rejected CLEAR leaves no partial row.
        """
        normalized_blast = request.blast_class.strip().lower()
        valid_blast = {choice.value for choice in cls.BlastClass}
        if normalized_blast not in valid_blast:
            msg = f"Unknown blast_class {request.blast_class!r}; valid: {sorted(valid_blast)}"
            raise ClearIssuanceError(msg)

        normalized_verify = request.gh_verify_result.strip().lower()
        valid_verify = {choice.value for choice in cls.VerifyResult}
        if normalized_verify not in valid_verify:
            msg = f"Unknown gh_verify_result {request.gh_verify_result!r}; valid: {sorted(valid_verify)}"
            raise ClearIssuanceError(msg)

        reviewer = request.reviewer_identity.strip()
        if not reviewer:
            msg = "reviewer_identity is required and must be non-empty (§17.4.2)"
            raise ClearIssuanceError(msg)
        if reviewer == request.executing_loop_identity.strip():
            msg = (
                f"reviewer_identity {reviewer!r} equals the executing loop identity "
                f"({request.executing_loop_identity!r}) — a CLEAR must be issued by an independent "
                f"cold reviewer, never self-issued by the loop that will execute it (§17.8 clause 3)"
            )
            raise ClearIssuanceError(msg)
        if is_non_reviewer_role(reviewer):
            msg = (
                f"reviewer_identity {reviewer!r} is a maker/coding-agent/loop role — a CLEAR "
                f"must be issued by an independent cold reviewer, not self-attested (§17.8 clause 3)"
            )
            raise ClearIssuanceError(msg)

        if not is_commit_sha(request.reviewed_sha):
            msg = (
                f"reviewed_sha {request.reviewed_sha!r} is not a hex commit SHA — a CLEAR binds to "
                f"the exact reviewed tree, never a branch ref (§17.4.2)"
            )
            raise ClearIssuanceError(msg)

        authorizer = request.human_authorizer.strip()
        if authorizer and normalized_blast != cls.BlastClass.SUBSTRATE:
            msg = (
                f"human_authorizer is only valid with blast_class=substrate "
                f"(got {normalized_blast!r}); non-substrate CLEARs merge through the loop"
            )
            raise ClearIssuanceError(msg)

        with transaction.atomic():
            return cls.objects.create(
                ticket=request.ticket,
                pr_id=request.pr_id,
                slug=request.slug.strip(),
                reviewed_sha=request.reviewed_sha.strip(),
                reviewer_identity=reviewer,
                gh_verify_result=normalized_verify,
                blast_class=normalized_blast,
                human_authorizer=authorizer,
            )

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

    def is_substrate(self) -> bool:
        """True iff this CLEAR is for a substrate-class change (invariant 4)."""
        return self.blast_class == self.BlastClass.SUBSTRATE

    def human_merge_authorized_by(self, presented_authorizer: str) -> bool:
        """True iff a substrate CLEAR's recorded authoriser matches what the merge call presents.

        The substrate human-merge path (§17.4.3 step 5 / invariant 8) is only
        unlocked when (a) this CLEAR is substrate-class, (b) it carries a
        non-empty ``human_authorizer`` recorded by the orchestrator at issue
        time, and (c) the ``ticket merge`` invocation re-presents that exact
        authoriser. The presented value must match the recorded one so the
        human decision is bound to the merge, not merely asserted at the CLI.
        """
        presented = presented_authorizer.strip()
        return bool(self.is_substrate() and self.human_authorizer and presented == self.human_authorizer)


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
