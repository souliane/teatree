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
# when it touches the merge keystone, the architecture spec, a governance doc,
# or the factory's OWN self-governance seams. Those seams are: the merge/CLEAR
# classifier and the cold-review record that DEFINE the trust boundary itself
# (``merge_clear.py`` — this module — and ``review_verdict.py``, the maker≠checker
# guard), every merge/safety gate (``core/gates/``), the trust classifier
# (``author_trust.py``), the intake gate (``issue_implementer.py`` /
# ``scanner_factories.py``), the autonomy/trust configuration (``config/`` —
# autonomy tiers and the ``substrate_auto_merge_authorized_by`` default), the
# on-behalf authorisation gate (``on_behalf_gate.py``), and the PreToolUse/Stop
# safety hooks (``hooks/``). Schema migrations (``core/migrations/``, incl.
# destructive DROP / data-rewrites) are substrate too: they mutate the durable
# governance store itself — this gap is why #3464's migration auto-merged as
# logic. The label defaults to ``logic`` (the orchestrator's judgment), so a
# change a human forgot to mark would otherwise auto-merge silently under
# ``autonomy = full``. This path detector makes the substrate guarantee
# label-independent (invariant 4): the change is substrate if its diff is. The
# self-governance seams (#3244) are held because an autonomous PR that widened
# the trusted-author set, loosened a gate, edited the classifier that judges
# itself, or shipped a destructive migration must NEVER auto-merge itself on
# agent-only review — the factory cannot loosen its own guardrails unattended.
_SUBSTRATE_PATH_PREFIXES = (
    "src/teatree/core/merge/",
    "src/teatree/core/models/merge_clear.py",
    "src/teatree/core/models/review_verdict.py",
    "src/teatree/core/gates/",
    "src/teatree/core/migrations/",
    "src/teatree/core/review/author_trust.py",
    "src/teatree/config/",
    "src/teatree/on_behalf_gate.py",
    "src/teatree/loop/scanners/issue_implementer.py",
    "src/teatree/loop/scanner_factories.py",
    "hooks/",
    "docs/blueprint/",
)
_SUBSTRATE_FILE_NAMES = frozenset({"BLUEPRINT.md", "CLAUDE.md", "AGENTS.md"})


def diff_paths_are_substrate(paths: "Iterable[str]") -> bool:
    """True iff any of *paths* is a substrate path (keystone / spec / governance / self-governance / migrations).

    Substrate paths are: anything under ``src/teatree/core/merge/`` (the merge
    keystone), the architecture spec (``BLUEPRINT.md`` and ``docs/blueprint/``),
    the governance docs (``CLAUDE.md`` / ``AGENTS.md`` at any depth), the
    factory's self-governance seams (#3244) — the merge/CLEAR classifier and
    cold-review record that DEFINE the trust boundary (``merge_clear.py`` /
    ``review_verdict.py``), every merge/safety gate (``core/gates/``), the trust
    classifier (``author_trust.py``), the intake gate (``issue_implementer.py`` /
    ``scanner_factories.py``), the autonomy/trust config (``config/``), the
    on-behalf gate (``on_behalf_gate.py``) and the safety hooks (``hooks/``) — and
    schema migrations (``core/migrations/``, incl. destructive DROP / data
    rewrites, which mutate the durable governance store). Directory/file prefixes
    match after stripping a leading ``./`` or ``/``; the bare governance file
    names (``BLUEPRINT.md`` etc.) match on the final path component so a
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
    # The human-authorized PENDING-checks expedite waiver (§17.4.3 / PR-07). Both
    # are empty for a normal CLEAR. When set, ``issue()`` accepts a ``pending``
    # snapshot ONLY when the linked ticket is flagged expedited, a human authoriser
    # is recorded, and ``local_ci_green_sha`` is a full SHA equal to ``reviewed_sha``
    # (the local-full-CI-green attestation bound to the exact reviewed tree). A
    # ``failed`` snapshot is refused unconditionally — expedite can never waive it.
    expedite_authorizer: str = ""
    local_ci_green_sha: str = ""


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
    # The recorded human authoriser of a PENDING-checks expedite waiver (§17.4.3 /
    # PR-07), and the local-full-CI-green attestation SHA it is bound to. Both empty
    # for a normal CLEAR. Set together (via ``ticket clear --expedite-authorize
    # <id> --local-ci-green-sha <sha>``) they let a ``pending`` snapshot authorize a
    # merge — but ONLY when re-presented at merge time (``expedite_pending_waived_by``)
    # AND the attestation still equals ``reviewed_sha``. Orthogonal to
    # ``human_authorizer`` (the substrate key) so the two waivers never cross-unlock.
    expedite_authorizer = models.CharField(max_length=255, blank=True, default="")
    local_ci_green_sha = models.CharField(max_length=64, blank=True, default="")
    issued_at = models.DateTimeField(default=timezone.now)
    consumed_at = models.DateTimeField(null=True, blank=True)

    # Non-persisted: the diff paths the merge gate fetched live for this CLEAR.
    # Populated at merge time (``_assert_clear_authorized``) from the forge's
    # changed-file list so ``is_substrate()`` can detect a mislabeled substrate
    # diff. Not a DB column — no migration, no compaction-survival concern (it is
    # re-derived from the live PR each merge attempt).
    touched_paths: "tuple[str, ...]" = ()
    # Non-persisted: True when the live changed-path list could NOT be read to
    # completion (forge error, or a paginated/truncated diff). The substrate
    # detector can then no longer PROVE the diff is non-substrate, so it fails
    # CLOSED (``is_substrate()`` holds the merge) — a >100-file PR whose substrate
    # change sorted past a truncated page can never silently auto-merge.
    substrate_paths_indeterminate: bool = False

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
        # The recorded reviewer verdict must itself be merge-safe at the reviewed
        # tree (§17.4.2 / §17.8 clause 3 — maker≠checker; the checker's recorded
        # verdict is authoritative). Three-valued (ci_rollup green/pending/failed):
        #   * GREEN  — the normal merge-safe verdict.
        #   * FAILED — a real red verdict; can NEVER authorize a merge, and expedite
        #              can never waive it. Refused unconditionally.
        #   * PENDING — checks queued, no verdict yet. Issuable ONLY as a
        #              human-authorized, SHA-bound expedite waiver: the linked
        #              ticket flagged ``expedited``, ``expedite_authorizer``
        #              recorded, and ``local_ci_green_sha`` a full SHA equal to
        #              ``reviewed_sha`` (the local-full-CI-green attestation bound
        #              to the exact reviewed tree). The flag alone grants NO bypass.
        normalized_sha = request.reviewed_sha.strip().lower()
        expedite_authorizer = request.expedite_authorizer.strip()
        attestation_sha = request.local_ci_green_sha.strip().lower()
        cls._assert_verify_result_issuable(
            normalized_verify=normalized_verify,
            ticket=request.ticket,
            reviewed_sha=normalized_sha,
            expedite_authorizer=expedite_authorizer,
            attestation_sha=attestation_sha,
        )

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

        # ``normalized_sha`` is the canonical lowercase hex form (computed above),
        # so the merge-time equality gate against GitHub's lowercase ``headRefOid``
        # cannot silently fail on a mixed-case input (#1162).
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
                    expedite_authorizer=expedite_authorizer,
                    local_ci_green_sha=attestation_sha,
                )

        # #1520: a transient ``database is locked`` from a concurrent
        # canonical-DB writer must not abort CLEAR issuance (``ticket
        # clear``). All validation above has already passed; the single
        # row write retries on a momentary lock and surfaces a genuinely
        # stuck lock after the cap.
        return retry_on_locked(_create)

    @classmethod
    def _assert_verify_result_issuable(
        cls,
        *,
        normalized_verify: str,
        ticket: "Ticket | None",
        reviewed_sha: str,
        expedite_authorizer: str,
        attestation_sha: str,
    ) -> None:
        """Enforce the three-valued verify-result gate for :meth:`issue` (§17.4.3 / PR-07).

        ``failed`` is refused unconditionally (expedite can never waive a real red
        verdict). When the expedite fields are present (on a ``green`` or ``pending``
        snapshot alike) they must form a complete, tree-bound waiver: a linked ticket
        flagged ``expedited``, a recorded authoriser, and a full-SHA attestation equal
        to ``reviewed_sha``. A ``pending`` snapshot is issuable ONLY as such a waiver;
        a ``green`` snapshot with expedite fields pre-authorises a later queue-flip.
        Raises :class:`ClearIssuanceError` with a precise reason on the first failure.
        """
        if normalized_verify == cls.VerifyResult.FAILED:
            msg = (
                "gh_verify_result 'failed' can never authorize a merge — a FAILED required "
                "check is a real red verdict, and the expedite waiver can only waive a "
                "PENDING (queued) check, never a FAILED one (§17.4.2 / §17.8 clause 3). "
                "Fix the failing check and re-review at the current SHA"
            )
            raise ClearIssuanceError(msg)

        has_expedite = bool(expedite_authorizer or attestation_sha)
        if has_expedite:
            if ticket is None or not ticket.may_expedite():
                msg = (
                    "expedite fields (expedite_authorizer / local_ci_green_sha) require a linked "
                    "ticket flagged expedited — the flag alone grants NO bypass, it only makes the "
                    "per-CLEAR human-authorized pending-waiver issuable. Flag it with "
                    "`t3 <overlay> ticket expedite <id>` and pass `--ticket-id <id>`"
                )
                raise ClearIssuanceError(msg)
            if not expedite_authorizer:
                msg = (
                    "local_ci_green_sha was given without expedite_authorizer — a pending-waiver "
                    "requires a recorded human authoriser (`--expedite-authorize <id>`)"
                )
                raise ClearIssuanceError(msg)
            if not is_commit_sha(attestation_sha) or attestation_sha != reviewed_sha:
                msg = (
                    f"local_ci_green_sha {attestation_sha!r} must be a full {SHA_FULL_LEN}-char hex "
                    f"SHA EQUAL to reviewed_sha {reviewed_sha!r} — the local-full-CI-green attestation "
                    f"binds the waiver to the exact reviewed tree; a truncated or divergent SHA is "
                    f"replayable against a different tree and is refused (mirrors the CLEAR's own "
                    f"#1829 SHA-bind design)"
                )
                raise ClearIssuanceError(msg)

        if normalized_verify == cls.VerifyResult.PENDING and not has_expedite:
            msg = (
                "gh_verify_result 'pending' (checks queued, no verdict yet) is issuable ONLY as a "
                "human-authorized, SHA-bound expedite waiver — pass `--expedite-authorize <id>` and "
                "`--local-ci-green-sha <reviewed-sha>` on a ticket flagged expedited, or re-review "
                "once checks are green (§17.4.3 / PR-07)"
            )
            raise ClearIssuanceError(msg)

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

        Substrate by ANY of: the recorded ``blast_class`` label, the live diff
        touching a substrate path (:func:`diff_paths_are_substrate` over
        :attr:`touched_paths`), OR an INDETERMINATE changed-path list
        (:attr:`substrate_paths_indeterminate`). The path detector makes the
        guarantee reliable — a substrate diff a human left at the default
        ``logic`` label is still held, never auto-merged — and the fail-closed
        indeterminate branch means a diff the forge could not read to completion
        (truncated/paginated/errored) is held for the owner rather than silently
        auto-merged as non-substrate.
        """
        return (
            self.blast_class == self.BlastClass.SUBSTRATE
            or self.substrate_paths_indeterminate
            or diff_paths_are_substrate(self.touched_paths)
        )

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

    def expedite_pending_waived_by(self, presented_authorizer: str) -> bool:
        """True iff this CLEAR carries a valid PENDING-checks waiver matching *presented_authorizer*.

        The PENDING-checks expedite path (§17.4.3 / PR-07) is unlocked only when
        (a) a non-empty ``expedite_authorizer`` was recorded at issue time, (b) the
        ``ticket merge`` invocation re-presents that exact authoriser, and (c) the
        tree-bound ``local_ci_green_sha`` (the local-full-CI-green attestation) still
        equals ``reviewed_sha`` — re-checked here so a force-push that moved the head
        (and, via the live SHA gate, the reviewed tree) invalidates the waiver. It
        governs ONLY the ``pending`` case: a FAILED required check is never waivable.
        Orthogonal to :meth:`human_merge_authorized_by` (the substrate key) so the two
        waivers can never cross-unlock — a presented ``--human-authorized`` never
        satisfies this, and a presented ``--expedite-authorized`` never satisfies that.
        """
        presented = presented_authorizer.strip()
        return bool(
            self.expedite_authorizer
            and presented == self.expedite_authorizer
            and self.local_ci_green_sha
            and self.local_ci_green_sha == self.reviewed_sha
        )


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
    # The #1335-reconciled ``owner/repo`` the gate actually merged against,
    # stamped at merge time inside ``record_merge_and_advance``'s atomic block
    # (#19). It is the merge-time truth the S1/S3 signal joins read FIRST — a
    # cross-repo merge is joined under its real repo, never the CLEAR's offline
    # workstream slug. Blank for legacy rows written before #19; the signal
    # resolver falls back to ``resolve_pr_repo_slug(clear)`` for those.
    repo_slug = models.CharField(max_length=255, blank=True, default="")
    # The expedite authoriser whose PENDING-checks waiver this merge actually used
    # (§17.4.3 / PR-07). Empty for every normal merge; non-empty only when the merge
    # proceeded on ``required_checks_status='pending'`` via the human-authorized
    # waiver — the durable audit trail of every expedited merge.
    expedited_by = models.CharField(max_length=255, blank=True, default="")
    # The config-sourced standing substrate authorizer id this merge used (#3413).
    # Empty for every merge EXCEPT a substrate merge authorized by the owner's
    # standing ``substrate_auto_merge_authorized_by`` delegation (as opposed to a
    # per-PR recorded ``human_authorizer``). The durable audit trail that keeps a
    # config-sourced standing-delegation merge distinguishable from an interactive
    # human authorization (invariant 4 audit).
    standing_delegation_by = models.CharField(max_length=255, blank=True, default="")

    class Meta:
        db_table = "teatree_merge_audit"
        ordering: ClassVar = ["-merged_at"]

    def __str__(self) -> str:
        return f"merge-audit<{self.clear.slug}#{self.clear.pr_id}@{self.merged_sha[:8]}>"
