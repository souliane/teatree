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

import re
from dataclasses import dataclass
from typing import TYPE_CHECKING, ClassVar

from django.db import models, transaction
from django.utils import timezone

from teatree.core.modelkit.db_retry import retry_on_locked
from teatree.core.models.ticket import Ticket

if TYPE_CHECKING:
    from collections.abc import Iterable

# §17.8 clause 3 / §17.6 candidate 13: an independent cold-review attestation
# cannot be recorded by the maker/coding-agent/loop side — the author would be
# rubber-stamping their own work. The CLEAR-issuer guard (this module) and the
# `reviewing`-attestation guard (lifecycle command) share this single list so
# they cannot drift apart. It lives on the model because the model owns the
# CLEAR contract (§17.4.2); the command layer imports it from here.
#
# Punctuated-prefix tokens ("maker:", "maker-", "coding-agent") are matched as
# leading prefixes. Bare role words ("maker", "coding", "loop") are matched
# when they appear as a delimited component (split on "-", ":", "_"), so the
# executor's canonical identity "merge-loop" is caught even though it does not
# *start* with "loop".
NON_REVIEWER_AGENT_PREFIXES = ("maker:", "maker-", "coding-agent", "coding", "loop")

_SHA_ALPHABET = frozenset("0123456789abcdef")
# A CLEAR binds to the exact reviewed tree (§17.4.2). An abbreviated SHA is
# ambiguous AND cannot satisfy the merge-time equality gate, which compares the
# stored ``reviewed_sha`` against the full 40-char ``headRefOid`` returned by
# ``gh pr view`` (#1162). A truncated SHA therefore produces an unmergeable
# CLEAR — refuse it at issuance.
SHA_FULL_LEN = 40


_COMPONENT_ROLE_WORDS = frozenset({"maker", "coding", "loop"})

# A diff is substrate — independent of the reviewer's ``blast_class`` label —
# when it touches the merge keystone, the architecture spec, or a governance
# doc. The label defaults to ``logic`` (the orchestrator's judgment), so a
# substrate change a human forgot to mark would otherwise auto-merge silently
# under ``autonomy = full``. This path detector makes the substrate guarantee
# label-independent (invariant 4): the change is substrate if its diff is.
_SUBSTRATE_PATH_PREFIXES = ("src/teatree/core/merge/", "docs/blueprint/")
_SUBSTRATE_FILE_NAMES = frozenset({"BLUEPRINT.md", "CLAUDE.md", "AGENTS.md"})


def diff_paths_are_substrate(paths: "Iterable[str]") -> bool:
    """True iff any of *paths* is a substrate path (merge keystone / spec / governance).

    Substrate paths are: anything under ``src/teatree/core/merge/`` (the merge
    keystone), the architecture spec (``BLUEPRINT.md`` and ``docs/blueprint/``),
    and the governance docs (``CLAUDE.md`` / ``AGENTS.md`` at any depth). Matching
    is on whole path components after stripping a leading ``./`` or ``/`` so a
    look-alike sibling (``BLUEPRINT.md.bak``, ``src/teatree/core/merger/``) is not
    misclassified.
    """
    for raw in paths:
        normalized = raw.strip().lstrip("/").removeprefix("./")
        if not normalized:
            continue
        if any(normalized.startswith(prefix) for prefix in _SUBSTRATE_PATH_PREFIXES):
            return True
        if normalized.rsplit("/", 1)[-1] in _SUBSTRATE_FILE_NAMES:
            return True
    return False


def is_non_reviewer_role(identity: str) -> bool:
    """True iff ``identity`` is a maker/coding-agent/loop role (§17.8 clause 3).

    Punctuated-prefix tokens ("maker:", "maker-", "coding-agent") are matched
    as leading prefixes. Bare role words ("maker", "coding", "loop") are also
    matched when they appear as any delimited component of the identity, so the
    executor's canonical identity "merge-loop" is blocked even though it does
    not start with "loop". Incidental substrings (e.g. "decoding") are not
    matched because the split honours delimiters only.
    """
    lowered = identity.strip().lower()
    if any(lowered == prefix or lowered.startswith(prefix) for prefix in NON_REVIEWER_AGENT_PREFIXES):
        return True
    parts = frozenset(re.split(r"[-:_]", lowered))
    return bool(parts & _COMPONENT_ROLE_WORDS)


def is_commit_sha(value: str) -> bool:
    """True iff ``value`` is a full 40-char hex commit SHA (§17.4.2, #1162).

    A CLEAR must bind to the exact reviewed tree, and the merge-time gate
    compares ``reviewed_sha`` against the full 40-char ``headRefOid`` from
    ``gh pr view``. Accepting an abbreviated SHA produces a CLEAR that can
    never satisfy the equality gate — the row is unmergeable from birth.
    Only the full 40-char SHA-1 is accepted (git's current default).
    """
    candidate = value.strip().lower()
    return len(candidate) == SHA_FULL_LEN and all(char in _SHA_ALPHABET for char in candidate)


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
    # Set ONLY for a substrate-class CLEAR a human/owner explicitly approved
    # for merge (§17.4.3 step 5 / invariant 4). Empty for every non-substrate
    # CLEAR and for an un-approved substrate CLEAR — the loop never
    # auto-merges substrate; a recorded human approval is the only way a
    # substrate CLEAR becomes mergeable. Approval is the gate — the AGENT
    # still executes the merge through the sanctioned ``t3 ... ticket merge``
    # transition (invariant 8), never raw ``gh``, and never a human-performed
    # merge action. This records *who approved* it so the FSM/audit stays
    # coherent.
    human_authorizer = models.CharField(max_length=255, blank=True, default="")
    issued_at = models.DateTimeField(default=timezone.now)
    consumed_at = models.DateTimeField(null=True, blank=True)

    # Non-persisted: the diff paths the merge gate fetched live for this CLEAR.
    # Populated at merge time (``_assert_clear_authorized``) from the forge's
    # changed-file list so ``is_substrate()`` can detect a mislabeled substrate
    # diff. Not a DB column — no migration, no compaction-survival concern (it is
    # re-derived from the live PR each merge attempt).
    touched_paths: "tuple[str, ...]" = ()

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
        # The recorded reviewer verdict must itself be merge-safe at the
        # reviewed tree. A CLEAR carrying a non-green verdict (the reviewer
        # recorded a HOLD — pending/failed checks at ``reviewed_sha``) must
        # never become an actionable authorization: issuing it would let the
        # merge-time gate's *live* re-check stamp green over the reviewer's
        # recorded HOLD if CI later flipped green on its own (§17.4.2 / §17.8
        # clause 3 — maker≠checker; the checker's recorded verdict is authoritative).
        if normalized_verify != cls.VerifyResult.GREEN:
            msg = (
                f"gh_verify_result {normalized_verify!r} is not green — a CLEAR records the "
                f"reviewer's merge-safe verdict at the reviewed tree; a HOLD (pending/failed) "
                f"verdict can never authorize a merge (§17.4.2 / §17.8 clause 3). Re-review at "
                f"the current SHA once checks are green, then issue a fresh CLEAR"
            )
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
            candidate = request.reviewed_sha.strip()
            msg = (
                f"reviewed_sha {request.reviewed_sha!r} (length={len(candidate)}) is not a full "
                f"{SHA_FULL_LEN}-char hex commit SHA — a CLEAR binds to the exact reviewed tree, "
                f"never a branch ref or abbreviated SHA (§17.4.2, #1162). The merge-time gate "
                f"compares against GitHub's full ``headRefOid`` returned by ``gh pr view``; a "
                f"truncated SHA can never match. Pass the full 40-char SHA — e.g. the output "
                f"of ``git rev-parse HEAD`` or ``gh pr view <id> --json headRefOid``"
            )
            raise ClearIssuanceError(msg)

        authorizer = request.human_authorizer.strip()
        if authorizer and normalized_blast != cls.BlastClass.SUBSTRATE:
            msg = (
                f"human_authorizer is only valid with blast_class=substrate "
                f"(got {normalized_blast!r}); non-substrate CLEARs merge through the loop"
            )
            raise ClearIssuanceError(msg)

        # Store the canonical lowercase hex form so the merge-time
        # equality gate against GitHub's lowercase ``headRefOid`` cannot
        # silently fail on a mixed-case input (#1162). ``is_commit_sha``
        # already lowercases for validation; persist the same form.
        normalized_sha = request.reviewed_sha.strip().lower()

        def _create() -> "MergeClear":
            with transaction.atomic():
                return cls.objects.create(
                    ticket=request.ticket,
                    pr_id=request.pr_id,
                    slug=request.slug.strip(),
                    reviewed_sha=normalized_sha,
                    reviewer_identity=reviewer,
                    gh_verify_result=normalized_verify,
                    blast_class=normalized_blast,
                    human_authorizer=authorizer,
                )

        # #1520: a transient ``database is locked`` from a concurrent
        # canonical-DB writer must not abort CLEAR issuance (``ticket
        # clear``). All validation above has already passed; the single
        # row write retries on a momentary lock and surfaces a genuinely
        # stuck lock after the cap.
        return retry_on_locked(_create)

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
        """True iff this CLEAR is for a substrate-class change (invariant 4).

        Substrate by EITHER the recorded ``blast_class`` label OR the live diff
        touching a substrate path (:func:`diff_paths_are_substrate` over
        :attr:`touched_paths`). The path detector makes the guarantee reliable —
        a substrate diff a human left at the default ``logic`` label is still
        held, never auto-merged.
        """
        return self.blast_class == self.BlastClass.SUBSTRATE or diff_paths_are_substrate(self.touched_paths)

    def human_merge_authorized_by(self, presented_authorizer: str) -> bool:
        """True iff a substrate CLEAR's recorded authoriser matches what the merge call presents.

        The substrate approval path (§17.4.3 step 5 / invariant 8) is only
        unlocked when (a) this CLEAR is substrate-class, (b) it carries a
        non-empty ``human_authorizer`` recorded by the orchestrator at issue
        time, and (c) the ``ticket merge`` invocation re-presents that exact
        authoriser. The presented value must match the recorded one so the
        human *approval* is bound to the merge (the agent then executes it),
        not merely asserted at the CLI.
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
