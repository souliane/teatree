"""Durable per-MR cold-review verdict so a verdict is recorded once, not re-derived.

A cold review re-derives the same merge-safe/hold judgment from scratch on
every session, which is wasteful and risks two sessions reaching inconsistent
verdicts for the same tree. ``ReviewVerdict`` persists the outcome keyed by
``(slug, pr_id, reviewed_sha)`` so a cheap lookup (``t3 <overlay> review
status``) can answer "is this PR safe to approve at its current head?" without
re-running a full cold review.

The verdict record is the read-side sibling of the ``MergeClear`` issuance
(BLUEPRINT §17.4.2): a CLEAR authorises *exactly one* merge and is single-use
(``consumed_at``); a ``ReviewVerdict`` is the durable *record of the review
judgment* and is queried repeatedly. The CLEAR-issuing path records a
``merge_safe`` verdict as a natural by-product; a HOLD verdict (which a CLEAR
can never carry — issuance refuses a non-green CLEAR) is recorded directly via
``review record``. Both share ``MergeClear``'s validation primitives
(``is_commit_sha``, blast/verify normalisation) so the two contracts cannot
drift apart.

Recording a verdict also resolves the PR's :class:`~teatree.core.models.mr_review_lock.MRReviewLock`
(#1405), in the same transaction as the row insert: whether the verdict is
``merge_safe`` or ``hold``, the in-flight review it concludes is no longer
"dispatched" — the MR's lock clears so a later push can dispatch a fresh
review, and the merge decision point's lock consult stops refusing the MR
this same verdict just vouched for (or held).
"""

import enum
from dataclasses import dataclass
from typing import ClassVar, TypedDict

from django.db import models, transaction
from django.utils import timezone

from teatree.core.models.merge_clear import SHA_FULL_LEN, MergeClear, is_commit_sha, is_non_reviewer_role
from teatree.core.models.mr_review_lock import MRReviewLock
from teatree.core.models.ticket import Ticket


class ReviewVerdictError(ValueError):
    """A ``ReviewVerdict`` was rejected at record time — the contract failed."""


class HeadVerdictState(enum.Enum):
    """The effective (newest-wins) verdict state among a PR's non-stale verdicts at a head.

    The merge gate's three outcomes (#2829): ``NO_MERGE_SAFE`` fails closed
    (requirement a — no recorded independent merge_safe vouches for the live
    head); ``HOLD`` re-blocks (requirement b — the most-recent non-stale
    verdict is a HOLD not superseded by a later merge_safe); ``MERGE_SAFE``
    allows (the latest verdict at the head is merge_safe). The "newest-wins"
    rule is the user-chosen semantic: a later PASS overrides an earlier HOLD,
    an even-later HOLD re-blocks.
    """

    NO_MERGE_SAFE = "no_merge_safe"
    HOLD = "hold"
    MERGE_SAFE = "merge_safe"


class FindingDict(TypedDict):
    """The JSONField-serialised shape of one :class:`Finding`."""

    severity: str
    summary: str
    file: str
    line: int


def _coerce_line(value: object) -> int:
    if isinstance(value, int):
        return value
    if isinstance(value, str) and value.isdigit():
        return int(value)
    return 0


@dataclass(frozen=True, slots=True)
class Finding:
    """One structured cold-review finding: severity + ``file:line`` + summary.

    Serialised to / from the ``findings`` JSONField as a :class:`FindingDict`
    so the record survives compaction in the canonical DB tier. ``line`` is
    ``0`` for a file-level (non-line-anchored) finding; ``file`` is empty for
    an MR-level one.
    """

    severity: str
    summary: str
    file: str = ""
    line: int = 0

    def as_dict(self) -> FindingDict:
        return {"severity": self.severity, "summary": self.summary, "file": self.file, "line": self.line}

    @classmethod
    def from_dict(cls, raw: dict) -> "Finding":
        return cls(
            severity=str(raw.get("severity", "")),
            summary=str(raw.get("summary", "")),
            file=str(raw.get("file", "")),
            line=_coerce_line(raw.get("line")),
        )

    def location(self) -> str:
        if self.file and self.line:
            return f"{self.file}:{self.line}"
        return self.file or "(MR-level)"


class Severity(models.TextChoices):
    BLOCKER = "blocker", "Blocker"
    MAJOR = "major", "Major"
    MINOR = "minor", "Minor"
    NIT = "nit", "Nit"


class ReviewVerdictManager(models.Manager["ReviewVerdict"]):
    """Read surface for the recorded-verdict lookup (``review status``)."""

    def for_pr(self, slug: str, pr_id: int) -> "models.QuerySet[ReviewVerdict]":
        return self.filter(slug=slug.strip(), pr_id=pr_id)

    def latest_for_pr(self, slug: str, pr_id: int) -> "ReviewVerdict | None":
        """The most recently recorded verdict for a PR, regardless of SHA.

        Ordered by ``recorded_at`` descending (the model's default ordering),
        so the first row is the freshest judgment — the one ``review status``
        reports against the PR's live head.
        """
        return self.for_pr(slug, pr_id).first()

    def effective_state_at(self, *, slug: str, pr_id: int, head_sha: str) -> "HeadVerdictState":
        """The newest-wins verdict state among the NON-STALE verdicts at *head_sha* (#2829).

        The effective verdict is the most-recent non-stale verdict by its
        recorded timestamp: ALLOW iff a non-stale ``merge_safe`` exists whose
        timestamp is ``>=`` every non-stale ``hold``'s (a later PASS overrides
        an earlier HOLD; an even-later HOLD re-blocks). A ``>=`` (not ``>``)
        comparison makes a same-timestamp tie resolve to PASS. *head_sha* is
        normalised the way :meth:`ReviewVerdict.is_stale_at` stores it.

        Returns :attr:`HeadVerdictState.NO_MERGE_SAFE` when no non-stale
        merge_safe exists (fail closed), :attr:`HeadVerdictState.HOLD` when the
        latest non-stale verdict is a HOLD, else
        :attr:`HeadVerdictState.MERGE_SAFE`. Shared by the merge-time gate
        (:func:`teatree.core.merge.authorization.assert_review_verdict_gate`)
        and the solo-sweep predicate
        (:func:`teatree.loop.scanners.pr_sweep_decision.has_independent_cold_review`)
        so the two cannot drift.
        """
        head = head_sha.strip().lower()
        non_stale = [verdict for verdict in self.for_pr(slug, pr_id) if not verdict.is_stale_at(head)]
        merge_safe_times = [verdict.recorded_at for verdict in non_stale if verdict.is_merge_safe()]
        if not merge_safe_times:
            return HeadVerdictState.NO_MERGE_SAFE
        hold_times = [verdict.recorded_at for verdict in non_stale if not verdict.is_merge_safe()]
        if hold_times and max(hold_times) > max(merge_safe_times):
            return HeadVerdictState.HOLD
        return HeadVerdictState.MERGE_SAFE


class ReviewVerdict(models.Model):
    """One recorded cold-review judgment for a PR at an exact reviewed tree.

    Keyed by ``(slug, pr_id, reviewed_sha)``: a fresh review at a moved head
    records a new row rather than mutating the old one, so the head-drift
    detection (:meth:`is_stale_at`) can compare the recorded ``reviewed_sha``
    against the forge's live head. ``verdict`` is the merge-safe/hold judgment;
    ``findings`` is the structured list the reviewer surfaced; the
    ``blast_class`` / ``gh_verify_result`` snapshot mirrors the ``MergeClear``
    fields so the record is a faithful sibling of the CLEAR contract.
    """

    class Verdict(models.TextChoices):
        MERGE_SAFE = "merge_safe", "Merge-safe"
        HOLD = "hold", "Hold"

    #: The Slack review-DONE reaction set per verdict (#113/#88): the loop
    #: reacts ``:eyes:`` (review finished — never at claim time) plus the
    #: verdict emoji. A clean / approvable review adds ``:white_check_mark:``;
    #: a review with blocking comments the author must address adds
    #: ``:question:``. The GitLab inline comments are the substance — the
    #: reaction is the ONLY Slack signal, never an author DM.
    DONE_EMOJIS: ClassVar[dict[str, tuple[str, ...]]] = {
        Verdict.MERGE_SAFE: ("eyes", "white_check_mark"),
        Verdict.HOLD: ("eyes", "question"),
    }

    ticket = models.ForeignKey(
        Ticket,
        on_delete=models.CASCADE,
        related_name="review_verdicts",
        null=True,
        blank=True,
    )
    pr_id = models.IntegerField()
    slug = models.CharField(max_length=255)
    reviewed_sha = models.CharField(max_length=64)
    verdict = models.CharField(max_length=16, choices=Verdict.choices)
    reviewer_identity = models.CharField(max_length=255)
    blast_class = models.CharField(max_length=16, choices=MergeClear.BlastClass.choices)
    gh_verify_result = models.CharField(max_length=32, choices=MergeClear.VerifyResult.choices)
    findings = models.JSONField(default=list, blank=True)
    recorded_at = models.DateTimeField(default=timezone.now)

    objects: ClassVar[ReviewVerdictManager] = ReviewVerdictManager()

    class Meta:
        db_table = "teatree_review_verdict"
        ordering: ClassVar = ["-recorded_at"]
        indexes: ClassVar = [models.Index(fields=["slug", "pr_id", "reviewed_sha"])]

    def __str__(self) -> str:
        return f"review-verdict<{self.slug}#{self.pr_id}@{self.reviewed_sha[:8]} {self.verdict}>"

    @classmethod
    # ast-grep-ignore: ac-django-no-complexity-suppressions
    def record(  # noqa: PLR0913 — the §17.4.2-mirroring field set IS the public record contract, same rationale as MergeClear.issue's ClearRequest.
        cls,
        *,
        pr_id: int,
        slug: str,
        reviewed_sha: str,
        verdict: str,
        reviewer_identity: str,
        findings: list[Finding] | None = None,
        blast_class: str = MergeClear.BlastClass.LOGIC,
        gh_verify_result: str = MergeClear.VerifyResult.GREEN,
        ticket: Ticket | None = None,
        expedited: bool = False,
    ) -> "ReviewVerdict":
        """The single guarded factory for a recorded verdict.

        Validates before any row is written and raises
        :class:`ReviewVerdictError` with a precise reason on the first
        violation: a known ``verdict`` / ``blast_class`` / ``gh_verify_result``;
        a non-empty ``reviewer_identity``; a full 40-char hex ``reviewed_sha``
        (same bind-to-the-exact-tree rule ``MergeClear.issue`` enforces, so the
        live-head equality check in :meth:`is_stale_at` cannot silently fail).
        A ``merge_safe`` verdict must NOT carry a FAILED ``gh_verify_result`` — the
        same maker≠checker invariant that refuses a FAILED CLEAR (§17.8 clause 3):
        a recorded HOLD on red checks can never be promoted to merge-safe by a
        later live re-check. A PENDING snapshot is accepted on a ``merge_safe``
        verdict ONLY when ``expedited`` is set (the sibling record of the
        human-authorized, SHA-bound expedite waiver ``MergeClear.issue`` records).
        """
        normalized_verdict = verdict.strip().lower()
        valid_verdict = {choice.value for choice in cls.Verdict}
        if normalized_verdict not in valid_verdict:
            msg = f"Unknown verdict {verdict!r}; valid: {sorted(valid_verdict)}"
            raise ReviewVerdictError(msg)

        normalized_blast = blast_class.strip().lower()
        valid_blast = {choice.value for choice in MergeClear.BlastClass}
        if normalized_blast not in valid_blast:
            msg = f"Unknown blast_class {blast_class!r}; valid: {sorted(valid_blast)}"
            raise ReviewVerdictError(msg)

        normalized_verify = gh_verify_result.strip().lower()
        valid_verify = {choice.value for choice in MergeClear.VerifyResult}
        if normalized_verify not in valid_verify:
            msg = f"Unknown gh_verify_result {gh_verify_result!r}; valid: {sorted(valid_verify)}"
            raise ReviewVerdictError(msg)
        if normalized_verdict == cls.Verdict.MERGE_SAFE:
            if normalized_verify == MergeClear.VerifyResult.FAILED:
                msg = (
                    f"a merge_safe verdict can never carry gh_verify_result=failed (got "
                    f"{normalized_verify!r}) — a FAILED required check is a real red verdict and "
                    f"expedite can never waive it (§17.8 clause 3; mirrors MergeClear.issue refusing "
                    f"a failed CLEAR)"
                )
                raise ReviewVerdictError(msg)
            if normalized_verify == MergeClear.VerifyResult.PENDING and not expedited:
                msg = (
                    f"a merge_safe verdict on PENDING checks (got {normalized_verify!r}) requires the "
                    f"expedite waiver (expedited=True) — a recorded HOLD on queued checks can never be "
                    f"promoted to merge-safe by a later live re-check unless the CLEAR carries a "
                    f"human-authorized, SHA-bound pending-waiver (§17.8 clause 3)"
                )
                raise ReviewVerdictError(msg)

        reviewer = reviewer_identity.strip()
        if not reviewer:
            msg = "reviewer_identity is required and must be non-empty"
            raise ReviewVerdictError(msg)
        if is_non_reviewer_role(reviewer):
            msg = (
                f"reviewer_identity {reviewer!r} is a maker/coding-agent/loop role — a verdict "
                f"records an independent cold review, never a self-attestation (§17.8 clause 3; "
                f"mirrors MergeClear.issue rejecting a non-reviewer CLEAR author)"
            )
            raise ReviewVerdictError(msg)

        if not is_commit_sha(reviewed_sha):
            candidate = reviewed_sha.strip()
            msg = (
                f"reviewed_sha {reviewed_sha!r} (length={len(candidate)}) is not a full "
                f"{SHA_FULL_LEN}-char hex commit SHA — a verdict binds to the exact reviewed tree so "
                f"the live-head equality check can compare it against the forge's headRefOid. Pass the "
                f"full 40-char SHA (e.g. `git rev-parse HEAD`)"
            )
            raise ReviewVerdictError(msg)

        with transaction.atomic():
            recorded = cls.objects.create(
                ticket=ticket,
                pr_id=pr_id,
                slug=slug.strip(),
                reviewed_sha=reviewed_sha.strip().lower(),
                verdict=normalized_verdict,
                reviewer_identity=reviewer,
                blast_class=normalized_blast,
                gh_verify_result=normalized_verify,
                findings=[finding.as_dict() for finding in (findings or [])],
            )
            MRReviewLock.resolve(slug=recorded.slug, pr_id=recorded.pr_id)
            return recorded

    @property
    def structured_findings(self) -> list[Finding]:
        return [Finding.from_dict(raw) for raw in self.findings if isinstance(raw, dict)]

    def is_merge_safe(self) -> bool:
        return self.verdict == self.Verdict.MERGE_SAFE

    def is_stale_at(self, current_head_sha: str) -> bool:
        """True iff the PR's live head has moved off the reviewed tree.

        A stale verdict reviewed a tree the PR no longer points at — its
        judgment cannot vouch for the current head, so ``review status``
        reports it as needing a re-review.
        """
        return self.reviewed_sha != current_head_sha.strip().lower()

    def is_safe_to_approve_at(self, current_head_sha: str, *, live_checks_status: str) -> bool:
        """True iff this verdict vouches for approving the PR at its current head.

        Three conditions, all required: the recorded ``verdict`` is
        ``merge_safe``, the recorded ``reviewed_sha`` still equals the live
        head (not stale), and the forge's live required-checks rollup is green.
        The live checks re-check (not the recorded ``gh_verify_result``
        snapshot) is authoritative — the same rule the merge-time gate uses.
        """
        return (
            self.is_merge_safe()
            and not self.is_stale_at(current_head_sha)
            and live_checks_status.strip().lower() == MergeClear.VerifyResult.GREEN
        )

    def done_reaction_emojis(self) -> tuple[str, ...]:
        """The ``:eyes:`` + verdict emoji set to post on the MR's Slack message (#113/#88)."""
        return self.DONE_EMOJIS.get(self.verdict, ("eyes",))
