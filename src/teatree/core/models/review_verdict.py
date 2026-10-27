"""Durable per-MR cold-review verdict so a verdict is recorded once, not re-derived.

A cold review re-derives the same merge-safe/hold judgment from scratch on
every session, which is wasteful and risks two sessions reaching inconsistent
verdicts for the same tree. ``ReviewVerdict`` persists the outcome keyed by
``(slug, pr_id, reviewed_sha, reviewer, verdict)`` so a cheap lookup (``t3 <overlay> review
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
from typing import ClassVar, Final

from django.db import models, transaction
from django.utils import timezone

from teatree.core.modelkit.diff_scope import ChangedFileSet, out_of_scope_refusal
from teatree.core.modelkit.forge_readability import HEAD_SHA_UNREADABLE, LiveChecksProbe, head_sha_unreadable
from teatree.core.models.auto_review_dispatch import AutoReviewDispatch
from teatree.core.models.checks_admission import ReviewVerdictError, assert_checks_admit_merge_safe, live_checks_reader
from teatree.core.models.codex_review_marker import CodexReviewMarker
from teatree.core.models.finding_standard import FindingKind
from teatree.core.models.head_carry_over import HeadCarryOver
from teatree.core.models.merge_clear import SHA_FULL_LEN, ClearIssuanceError, MergeClear, is_commit_sha
from teatree.core.models.mr_review_lock import MRReviewLock
from teatree.core.models.review_finding import Finding
from teatree.core.models.review_hold_disposition import ReviewHoldDisposition
from teatree.core.models.review_verdict_duplicate import refresh_duplicate_verdict
from teatree.core.models.reviewer_identity import (
    is_independent_reviewer_identity,
    normalize_reviewer_identity,
    unrecognised_reviewer_message,
)
from teatree.core.models.ticket import Ticket


class HeadVerdictState(enum.Enum):
    """No pass, a standing hold beside a pass, or an unheld pass at one head."""

    NO_MERGE_SAFE = "no_merge_safe"
    HOLD = "hold"
    MERGE_SAFE = "merge_safe"


class ReviewEvidenceGap(enum.StrEnum):
    CI_UNREADABLE = "ci_unreadable"
    TESTS_UNAVAILABLE = "tests_unavailable"
    PLAN_UNAVAILABLE = "plan_unavailable"


def _known_choice(value: str, choices: type[models.TextChoices], *, field: str) -> str:
    """The normalised *value*, or a refusal naming the valid set for *field*."""
    normalized = value.strip().lower()
    valid = {choice.value for choice in choices}
    if normalized not in valid:
        msg = f"Unknown {field} {value!r}; valid: {sorted(valid)}"
        raise ReviewVerdictError(msg)
    return normalized


def _validated_evidence_gaps(
    *, verdict: str, verify_result: str, findings: list[Finding], evidence_gaps: list[str] | None
) -> list[str]:
    if evidence_gaps is not None and (
        not isinstance(evidence_gaps, list) or any(not isinstance(gap, str) for gap in evidence_gaps)
    ):
        msg = "evidence_gaps must be a list of strings"
        raise ReviewVerdictError(msg)
    gaps = list(dict.fromkeys(evidence_gaps or []))
    if verdict == "unverified":
        invalid = set(gaps) - {gap.value for gap in ReviewEvidenceGap}
        if not gaps or invalid:
            msg = f"unverified verdict requires known evidence gaps; invalid: {sorted(invalid)}"
            raise ReviewVerdictError(msg)
        if verify_result == MergeClear.VerifyResult.FAILED or any(
            finding.kind == FindingKind.BLOCKING for finding in findings
        ):
            msg = "unverified verdict cannot conceal a confirmed failed check or blocking finding"
            raise ReviewVerdictError(msg)
    elif gaps:
        msg = "evidence gaps belong only to an unverified verdict"
        raise ReviewVerdictError(msg)
    return gaps


#: Every per-HEAD review claim a RECORDED verdict retires. Both, because a verdict is a
#: fact about the TREE — it covers that exact head however the review was armed, so every
#: claim on the head is spent and re-arming either would be churn. Held here because this
#: is the only module that already imports both.
#:
#: Deliberately NOT reused by the refusal terminal (#4530). A refusal is a fact about ONE
#: RUN — a reviewer whose verdict and checks report contradicted each other — so it may
#: retire only the claim that armed that run
#: (:attr:`~teatree.core.models.review_target.ReviewTarget.armed_by`). Walking this list
#: there let a codex-path refusal latch a dispatch claim whose reviewer had not run yet and
#: free the review lock it held; 328 of 444 dispatch rows share a head with a marker row,
#: so that reach-across was the common case, not the corner.
_PER_HEAD_REVIEW_CLAIMS: Final = (AutoReviewDispatch, CodexReviewMarker)


def resolve_head_claims(*, slug: str, pr_id: int, head_sha: str) -> None:
    """Retire every per-head claim a RECORDED verdict concludes.

    Called inside :meth:`ReviewVerdict.record`'s transaction: the verdict covers this
    exact tree, so re-arming either claim would be review churn.
    """
    for claim in _PER_HEAD_REVIEW_CLAIMS:
        claim.mark_resolved(slug=slug, pr_id=pr_id, head_sha=head_sha)


def _validated_reviewer(reviewer_identity: str) -> str:
    """The stripped reviewer identity, refused when empty or not an independent one."""
    reviewer = reviewer_identity.strip()
    if not reviewer:
        msg = "reviewer_identity is required and must be non-empty"
        raise ReviewVerdictError(msg)
    if not is_independent_reviewer_identity(reviewer):
        raise ReviewVerdictError(unrecognised_reviewer_message(reviewer, subject="a verdict", verb="recorded"))
    return reviewer


def _assert_full_sha(reviewed_sha: str) -> None:
    """A verdict binds to the exact reviewed tree, so an abbreviated SHA is refused."""
    if is_commit_sha(reviewed_sha):
        return
    candidate = reviewed_sha.strip()
    msg = (
        f"reviewed_sha {reviewed_sha!r} (length={len(candidate)}) is not a full "
        f"{SHA_FULL_LEN}-char hex commit SHA — a verdict binds to the exact reviewed tree so "
        f"the live-head equality check can compare it against the forge's headRefOid. Pass the "
        f"full 40-char SHA (e.g. `git rev-parse HEAD`)"
    )
    raise ReviewVerdictError(msg)


class ReviewVerdictManager(models.Manager["ReviewVerdict"]):
    """Read surface for the recorded-verdict lookup (``review status``)."""

    def for_pr(self, slug: str, pr_id: int) -> "models.QuerySet[ReviewVerdict]":
        """Every recorded verdict for *slug*``#``*pr_id*, matched case-INSENSITIVELY.

        A forge slug is case-insensitive, so a verdict recorded under ``Owner/Repo``
        vouches for the same PR the merge gate resolves as ``owner/repo``. Matching
        exactly made such a verdict invisible to
        :func:`~teatree.core.merge.authorization.assert_review_verdict_gate`, which
        then refused the merge permanently while ``review status`` still showed the
        merge_safe verdict. Every sibling resolver in the subsystem — the
        ``PullRequest`` lookup, the §15 sibling supersede, the merge-quality ticket
        resolver — reads ``__iexact`` for the same reason.
        """
        return self.filter(slug__iexact=slug.strip(), pr_id=pr_id)

    def slug_spelling_for(
        self, slug: str, pr_id: int, *, reviewed_sha: str, reviewer_identity_normalized: str, verdict: str
    ) -> str:
        """The slug an existing row for this idempotency key already stores, else *slug*.

        The case-insensitive half of the ``(slug, pr_id, reviewed_sha, reviewer, verdict)``
        uniqueness: the DB constraint is byte-exact, so re-recording under a
        differently-cased slug would stack a second row for one forge repo.
        """
        stored = (
            self.for_pr(slug, pr_id)
            .filter(
                reviewed_sha=reviewed_sha,
                reviewer_identity_normalized=reviewer_identity_normalized,
                verdict=verdict,
            )
            .values_list("slug", flat=True)
            .first()
        )
        return stored or slug.strip()

    def latest_for_pr(self, slug: str, pr_id: int) -> "ReviewVerdict | None":
        """The speaking verdict at the newest recorded head, regardless of live SHA."""
        newest = self.for_pr(slug, pr_id).first()
        if newest is None:
            return None
        return self.speaking_verdict_at(slug=slug, pr_id=pr_id, head_sha=newest.reviewed_sha)

    def verdicts_at(self, *, slug: str, pr_id: int, head_sha: str) -> list["ReviewVerdict"]:
        head = head_sha.strip().lower()
        return [verdict for verdict in self.for_pr(slug, pr_id) if not verdict.is_stale_at(head)]

    def speaking_verdict_at(self, *, slug: str, pr_id: int, head_sha: str) -> "ReviewVerdict | None":
        """Prefer the newest standing hold, then the newest pass at this head."""
        return next(iter(self.speaking_verdicts_at(slug=slug, pr_id=pr_id, head_sha=head_sha)), None)

    def speaking_verdicts_at(self, *, slug: str, pr_id: int, head_sha: str) -> list["ReviewVerdict"]:
        """Every standing hold, or the newest pass when no hold stands."""
        at_head = self.verdicts_at(slug=slug, pr_id=pr_id, head_sha=head_sha)
        holds = self.unreconciled_holds_at(slug=slug, pr_id=pr_id, head_sha=head_sha)
        if holds:
            return holds
        passed = next((verdict for verdict in at_head if verdict.is_merge_safe()), None)
        return [passed] if passed is not None else at_head[:1]

    def effective_state_at(self, *, slug: str, pr_id: int, head_sha: str) -> "HeadVerdictState":
        """A pass authorizes only when no hold stands at the same head."""
        at_head = self.verdicts_at(slug=slug, pr_id=pr_id, head_sha=head_sha)
        if self.unreconciled_holds_at(slug=slug, pr_id=pr_id, head_sha=head_sha):
            return HeadVerdictState.HOLD
        if any(verdict.is_merge_safe() for verdict in at_head):
            return HeadVerdictState.MERGE_SAFE
        return HeadVerdictState.NO_MERGE_SAFE

    def unreconciled_holds_at(
        self, *, slug: str, pr_id: int, head_sha: str, at_head: list["ReviewVerdict"] | None = None
    ) -> list["ReviewVerdict"]:
        """Every standing hold at this head, including one followed by a pass."""
        holds = [
            verdict
            for verdict in (
                at_head if at_head is not None else self.verdicts_at(slug=slug, pr_id=pr_id, head_sha=head_sha)
            )
            if verdict.stands_as_hold()
        ]
        if not holds:
            return []
        by_id = {hold.pk: hold for hold in holds}
        cleared = {
            row.hold_id
            for row in ReviewHoldDisposition.objects.filter(hold__in=holds)
            if row.covers(by_id[row.hold_id])
        }
        return [hold for hold in holds if hold.pk not in cleared]

    def standing_hold_remedy_at(self, *, slug: str, pr_id: int, head_sha: str) -> str:
        """The refusal for active holds at this head, or an empty string."""
        holds = self.unreconciled_holds_at(slug=slug, pr_id=pr_id, head_sha=head_sha)
        return standing_hold_remedy(holds) if holds else ""

    def assert_no_standing_hold_at(self, *, slug: str, pr_id: int, head_sha: str) -> None:
        refusal = self.standing_hold_remedy_at(slug=slug, pr_id=pr_id, head_sha=head_sha)
        if refusal:
            raise ClearIssuanceError(refusal)


def standing_hold_remedy(holds: list["ReviewVerdict"]) -> str:
    parts = []
    for hold in holds:
        answered = {point["index"] for row in ReviewHoldDisposition.objects.filter(hold=hold) for point in row.points}
        remaining = sorted(ReviewHoldDisposition.blocking_indices(hold) - answered)
        parts.append(f"#{hold.pk} unanswered {remaining}")
    points = ", ".join(parts)
    return (
        f"standing HOLD verdict(s) {points}; push a fix and get the new head reviewed "
        "or record an independent cited disposition"
    )


class ReviewVerdict(models.Model):
    """One recorded cold-review judgment for a PR at an exact reviewed tree.

    Keyed by ``(slug, pr_id, reviewed_sha, reviewer, verdict)``: a fresh review at a moved head
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
        UNVERIFIED = "unverified", "Unverified"

    #: The Slack review-DONE reaction set per verdict (#113/#88): the loop
    #: reacts ``:eyes:`` (review finished — never at claim time) plus the
    #: verdict emoji. A clean / approvable review adds ``:white_check_mark:``;
    #: a review with blocking comments the author must address adds
    #: ``:question:``. The GitLab inline comments are the substance — the
    #: reaction is the ONLY Slack signal, never an author DM.
    DONE_EMOJIS: ClassVar[dict[str, tuple[str, ...]]] = {
        Verdict.MERGE_SAFE: ("eyes", "white_check_mark"),
        Verdict.HOLD: ("eyes", "question"),
        Verdict.UNVERIFIED: ("eyes",),
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
    #: The idempotency key for "who reviewed this sha" (F8). The free-text
    #: ``reviewer_identity`` normalised via :func:`normalize_reviewer_identity`;
    #: a UniqueConstraint on ``(slug, pr_id, reviewed_sha, this, verdict)`` keeps
    #: same-verdict re-records idempotent without overwriting a HOLD with a PASS.
    reviewer_identity_normalized = models.CharField(max_length=255, blank=True, default="", db_index=True)
    blast_class = models.CharField(max_length=16, choices=MergeClear.BlastClass.choices)
    gh_verify_result = models.CharField(max_length=32, choices=MergeClear.VerifyResult.choices)
    findings = models.JSONField(default=list, blank=True)
    evidence_gaps = models.JSONField(default=list, db_default=[], blank=True)  # ty: ignore[no-matching-overload] — django-stubs omits JSONField.db_default
    recorded_at = models.DateTimeField(default=timezone.now)

    objects: ClassVar[ReviewVerdictManager] = ReviewVerdictManager()

    class Meta:
        db_table = "teatree_review_verdict"
        ordering: ClassVar = ["-recorded_at"]
        indexes: ClassVar = [models.Index(fields=["slug", "pr_id", "reviewed_sha"])]
        constraints: ClassVar = [
            models.UniqueConstraint(
                fields=["slug", "pr_id", "reviewed_sha", "reviewer_identity_normalized", "verdict"],
                name="uniq_review_verdict_slug_pr_sha_reviewer_verdict",
            ),
        ]

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
        evidence_gaps: list[str] | None = None,
        blast_class: str = MergeClear.BlastClass.LOGIC,
        gh_verify_result: str = MergeClear.VerifyResult.GREEN,
        ticket: Ticket | None = None,
        expedited: bool = False,
        lock_holder: str = "",
        changed_files: ChangedFileSet | None = None,
        merge_result_retake: bool = False,
        live_checks: LiveChecksProbe | None = None,
        conclude_review: bool = True,
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
        later live re-check. Supplying ``live_checks`` decides WHICH refusal
        that is: only a red the forge confirms at the reviewed SHA raises the
        terminal-eligible ``ChecksContradictionError`` (see
        :mod:`teatree.core.models.checks_admission`). A PENDING snapshot is
        accepted on a ``merge_safe`` verdict ONLY when ``expedited`` is set (the
        sibling record of the human-authorized, SHA-bound expedite waiver
        ``MergeClear.issue`` records).

        Supplying ``changed_files`` arms the #4251 diff-scope gate: a blocking
        finding citing a path the PR provably does not touch is refused, because
        a branch-only probe reports what ``main`` did to that file since the
        branch was cut. ``merge_result_retake`` is the reviewer's attestation
        that the finding was re-measured on the materialised merge result, which
        is what makes such a claim admissible. An omitted or UNAVAILABLE set
        never fires the gate — see :mod:`teatree.core.review.diff_scope_gate`.
        """
        normalized_verdict = _known_choice(verdict, cls.Verdict, field="verdict")
        normalized_blast = _known_choice(blast_class, MergeClear.BlastClass, field="blast_class")
        normalized_verify = _known_choice(gh_verify_result, MergeClear.VerifyResult, field="gh_verify_result")
        gaps = _validated_evidence_gaps(
            verdict=normalized_verdict,
            verify_result=normalized_verify,
            findings=findings or [],
            evidence_gaps=evidence_gaps,
        )
        if (
            normalized_verdict == cls.Verdict.HOLD
            and findings
            and normalized_verify != MergeClear.VerifyResult.FAILED
            and not any(finding.kind == FindingKind.BLOCKING for finding in findings)
            and any(finding.kind == FindingKind.NOTE for finding in findings)
        ):
            msg = "venue-only or documentation-only concern cannot hold this head; retry the review"
            raise ReviewVerdictError(msg)
        if normalized_verdict == cls.Verdict.MERGE_SAFE:
            assert_checks_admit_merge_safe(
                normalized_verify,
                expedited=expedited,
                read_live=live_checks_reader(live_checks, slug=slug, head_sha=reviewed_sha),
            )

        reviewer = _validated_reviewer(reviewer_identity)
        _assert_full_sha(reviewed_sha)
        scope_refusal = out_of_scope_refusal(
            findings or [],
            changed_files or ChangedFileSet.unavailable(),
            merge_result_retake=merge_result_retake,
        )
        if scope_refusal:
            raise ReviewVerdictError(scope_refusal)

        normalized_sha = reviewed_sha.strip().lower()
        normalized_reviewer = normalize_reviewer_identity(reviewer)
        with transaction.atomic():
            recorded, created = cls.objects.get_or_create(
                slug=cls.objects.slug_spelling_for(
                    slug,
                    pr_id,
                    reviewed_sha=normalized_sha,
                    reviewer_identity_normalized=normalized_reviewer,
                    verdict=normalized_verdict,
                ),
                pr_id=pr_id,
                reviewed_sha=normalized_sha,
                reviewer_identity_normalized=normalized_reviewer,
                verdict=normalized_verdict,
                defaults={
                    "ticket": ticket,
                    "reviewer_identity": reviewer,
                    "blast_class": normalized_blast,
                    "gh_verify_result": normalized_verify,
                    "findings": [finding.as_dict() for finding in (findings or [])],
                    "evidence_gaps": gaps,
                    "recorded_at": timezone.now(),
                },
            )
            if not created:
                recorded = cls.objects.select_for_update().get(pk=recorded.pk)
                refresh_duplicate_verdict(recorded, findings or [], gaps, normalized_verify)
            if conclude_review:
                recorded.conclude_review(lock_holder=lock_holder)
            return recorded

    def conclude_review(self, *, lock_holder: str) -> None:
        if self.verdict == self.Verdict.UNVERIFIED:
            for claim in _PER_HEAD_REVIEW_CLAIMS:
                claim.mark_unverified(slug=self.slug, pr_id=self.pr_id, head_sha=self.reviewed_sha)
        else:
            resolve_head_claims(slug=self.slug, pr_id=self.pr_id, head_sha=self.reviewed_sha)
        carried_descendant = HeadCarryOver.objects.filter(
            slug__iexact=self.slug, pr_id=self.pr_id, from_head=self.reviewed_sha
        ).filter(models.Q(outcome=HeadCarryOver.Outcome.CARRIED) | models.Q(reason="no_records"))
        if lock_holder or not carried_descendant.exists():
            MRReviewLock.resolve(slug=self.slug, pr_id=self.pr_id, holder=lock_holder)
        if self.stands_as_hold():
            self.carry_late_hold()

    def carry_late_hold(self) -> None:
        seen = {self.reviewed_sha}
        pending = [self.reviewed_sha]
        while pending:
            rows = HeadCarryOver.objects.filter(
                slug__iexact=self.slug, pr_id=self.pr_id, from_head=pending.pop()
            ).filter(models.Q(outcome=HeadCarryOver.Outcome.CARRIED) | models.Q(reason="no_records"))
            for row in rows:
                if row.to_head not in seen:
                    self.carry_forward(reviewed_sha=row.to_head)
                    seen.add(row.to_head)
                    pending.append(row.to_head)

    def carry_forward(self, *, reviewed_sha: str) -> "ReviewVerdict":
        """Re-record this verdict at *reviewed_sha*, preserving the expedite waiver.

        A carry-forward is a fresh row at the new tree keeping the ORIGINAL
        reviewer identity and every snapshot field — the reusable primitive both
        the conflict-only rebind (§17.4) and a superseding sibling-CLEAR use so
        neither hand-rolls a divergent verdict-copy path. The waiver is
        re-passed (``expedited=`` derived from the source's PENDING snapshot)
        because :meth:`record` refuses a merge_safe verdict on PENDING checks
        without it: a PENDING merge_safe row can only ever have been persisted
        under the human-authorized, SHA-bound waiver (§17.8 clause 3), so
        re-asserting it here re-binds the existing clearance, never grants a new
        one. :class:`ReviewVerdictError` propagates on a genuinely-unwaivable
        source row so the caller can refuse cleanly rather than crash.
        """
        with transaction.atomic():
            carried = self.record(
                pr_id=self.pr_id,
                slug=self.slug,
                reviewed_sha=reviewed_sha,
                verdict=self.verdict,
                reviewer_identity=self.reviewer_identity,
                findings=self.structured_findings,
                evidence_gaps=self.evidence_gaps,
                blast_class=self.blast_class,
                gh_verify_result=self.gh_verify_result,
                ticket=self.ticket,
                expedited=(self.gh_verify_result == MergeClear.VerifyResult.PENDING),
                conclude_review=False,
            )
            for disposition in ReviewHoldDisposition.objects.filter(hold=self).select_related("clearing_verdict"):
                clearing = disposition.clearing_verdict.carry_forward(reviewed_sha=reviewed_sha)
                disposition.carry_forward(hold=carried, clearing_verdict=clearing)
            return carried

    @property
    def structured_findings(self) -> list[Finding]:
        return [Finding.from_dict(raw) for raw in self.findings if isinstance(raw, dict)]

    def is_merge_safe(self) -> bool:
        return self.verdict == self.Verdict.MERGE_SAFE

    def stands_as_hold(self) -> bool:
        return self.verdict == self.Verdict.HOLD

    @staticmethod
    def is_head_unreadable(current_head_sha: str) -> bool:
        """True iff the forge named no head at all, so no staleness question can be answered.

        The guard a REPORTING caller owes its reader before it reaches for
        :meth:`is_stale_at`: a verdict on an unchanged tree is not invalidated by a
        failed read OF that tree, and calling one stale on a forge hiccup spends a
        full cold re-review on a pull request nobody touched.
        """
        return head_sha_unreadable(current_head_sha)

    def is_stale_at(self, current_head_sha: str) -> bool:
        """True iff the PR's live head has moved off the reviewed tree.

        A stale verdict reviewed a tree the PR no longer points at — its
        judgment cannot vouch for the current head, so ``review status``
        reports it as needing a re-review.

        The explicit UNREADABLE sentinel is REFUSED rather than answered: it is a
        non-answer about the head, and quietly returning True for it is precisely
        the "unchanged PR reported stale" defect. Ask :meth:`is_head_unreadable`
        first. A merely EMPTY head still answers True — every gate caller
        (:meth:`ReviewVerdictQuerySet.effective_state_at` and its siblings) rests on
        that fail-CLOSED reading, and a scanner tick must not learn to raise.
        """
        if current_head_sha == HEAD_SHA_UNREADABLE:
            msg = "is_stale_at cannot answer for an UNREADABLE head — ask is_head_unreadable first"
            raise ReviewVerdictError(msg)
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
