"""Per-ticket review-evidence artifact gating review-request and cross-repo close.

Two artifact kinds share one append-only table (PR-08 / migration M3):

* ``COLD_REVIEW`` — the review-evidence artifact the review-request broadcast
    is gated on. A broadcast is refused unless the ticket is ``REVIEWED`` *and*
    a cold-review evidence row exists (reviewer identity, verdict, head SHA,
    timestamp). The gate also accepts an existing
    :class:`~teatree.core.models.review_verdict.ReviewVerdict` as equivalent
    evidence, so the normal cold-review step — which already records a
    ``ReviewVerdict`` — satisfies the gate with no change to that path.
* ``INTEGRATION_REVIEW`` — the second artifact kind: an integration review of
    the *combined* changeset when a ticket touches ≥ 2 repos. Ticket close
    (``mark_delivered``) is gated on an integration-review row whose ``repos``
    cover the ticket's repos.

Mirrors the ``MergeClear`` / ``ReviewVerdict`` / ``PlanArtifact`` shape: a
dedicated durable row alongside ``Ticket``, an append-only guarded factory
(:meth:`record`) that validates before any row is written, and the same
maker≠checker + full-SHA primitives from ``merge_clear`` so a self-attested or
tree-unbound artifact can never advance a gate.
"""

from typing import ClassVar

from django.db import models, transaction
from django.utils import timezone

from teatree.core.models.merge_clear import SHA_FULL_LEN, is_commit_sha, is_non_reviewer_role
from teatree.core.models.ticket import Ticket

_MIN_INTEGRATION_REPOS = 2


class ReviewEvidenceError(ValueError):
    """A ``ReviewEvidence`` row was rejected at record time — the contract failed."""


def _normalize_repos(repos: list[str] | None) -> list[str]:
    """Strip, drop blanks, de-duplicate while preserving first-seen order."""
    seen: dict[str, None] = {}
    for repo in repos or []:
        cleaned = str(repo).strip()
        if cleaned and cleaned not in seen:
            seen[cleaned] = None
    return list(seen)


class ReviewEvidenceManager(models.Manager["ReviewEvidence"]):
    """Read surface for the two review-gate lookups."""

    def for_ticket(self, ticket: "Ticket", kind: str = "") -> "models.QuerySet[ReviewEvidence]":
        qs = self.filter(ticket=ticket)
        return qs.filter(kind=kind) if kind else qs

    def has_cold_review(self, ticket: "Ticket") -> bool:
        """Whether a cold-review evidence row exists for the ticket."""
        return self.for_ticket(ticket, ReviewEvidence.Kind.COLD_REVIEW).exists()

    def has_integration_review_covering(self, ticket: "Ticket", repos: list[str]) -> bool:
        """Whether an integration-review row covers every repo in *repos*.

        Coverage is set-inclusion: a single integration review must cover the
        whole combined changeset, so ``set(repos) <= set(row.repos)``. An empty
        *repos* is trivially covered (no cross-repo obligation).
        """
        required = set(_normalize_repos(repos))
        if not required:
            return True
        for row in self.for_ticket(ticket, ReviewEvidence.Kind.INTEGRATION_REVIEW):
            if required <= set(_normalize_repos(row.repos)):
                return True
        return False


class ReviewEvidence(models.Model):
    """One recorded review-evidence artifact for a ticket (cold or integration).

    Append-only: a fresh review records a new row, so the latest governs and
    the history is an immutable audit trail. ``repos`` records the changeset
    the review covered — a single repo for a cold review, the ≥ 2 combined
    repos for an integration review.
    """

    class Kind(models.TextChoices):
        COLD_REVIEW = "cold_review", "Cold review"
        INTEGRATION_REVIEW = "integration_review", "Integration review"

    ticket = models.ForeignKey(
        Ticket,
        on_delete=models.CASCADE,
        related_name="review_evidences",
    )
    kind = models.CharField(max_length=32, choices=Kind.choices)
    reviewer_identity = models.CharField(max_length=255)
    verdict = models.CharField(max_length=32)
    head_sha = models.CharField(max_length=64)
    repos = models.JSONField(default=list, blank=True)
    recorded_at = models.DateTimeField(default=timezone.now)

    objects: ClassVar[ReviewEvidenceManager] = ReviewEvidenceManager()

    class Meta:
        db_table = "teatree_review_evidence"
        ordering: ClassVar = ["-recorded_at"]
        indexes: ClassVar = [models.Index(fields=["ticket", "kind", "recorded_at"])]

    def __str__(self) -> str:
        return f"review-evidence<ticket:{self.ticket_id} {self.kind}@{self.head_sha[:8]}>"  # type: ignore[attr-defined]

    @classmethod
    # ast-grep-ignore: ac-django-no-complexity-suppressions
    def record(  # noqa: PLR0913 — the artifact field set IS the public record contract, same rationale as ReviewVerdict.record.
        cls,
        *,
        ticket: "Ticket",
        kind: str,
        reviewer_identity: str,
        verdict: str,
        head_sha: str,
        repos: list[str] | None = None,
    ) -> "ReviewEvidence":
        """The single guarded factory for a review-evidence row.

        Validates before any row is written and raises
        :class:`ReviewEvidenceError` on the first violation: a known ``kind``;
        a non-empty ``verdict``; a non-empty ``reviewer_identity`` that is not a
        maker/coding-agent/loop role (an evidence row records an *independent*
        review, never a self-attestation — the same §17.8 clause-3 invariant
        ``ReviewVerdict.record`` enforces); a full 40-char hex ``head_sha`` (so
        the artifact binds to an exact tree). An ``INTEGRATION_REVIEW`` must
        cover ≥ 2 distinct repos — a single-repo "integration" review is
        vacuous. Construction is atomic so a rejected artifact leaves no row.
        """
        normalized_kind = kind.strip().lower()
        valid_kinds = {choice.value for choice in cls.Kind}
        if normalized_kind not in valid_kinds:
            msg = f"Unknown kind {kind!r}; valid: {sorted(valid_kinds)}"
            raise ReviewEvidenceError(msg)

        cleaned_verdict = verdict.strip()
        if not cleaned_verdict:
            msg = "verdict is required and must be non-empty"
            raise ReviewEvidenceError(msg)

        reviewer = reviewer_identity.strip()
        if not reviewer:
            msg = "reviewer_identity is required and must be non-empty"
            raise ReviewEvidenceError(msg)
        if is_non_reviewer_role(reviewer):
            msg = (
                f"reviewer_identity {reviewer!r} is a maker/coding-agent/loop role — a review-evidence "
                f"artifact records an independent review, never a self-attestation (§17.8 clause 3)"
            )
            raise ReviewEvidenceError(msg)

        if not is_commit_sha(head_sha):
            candidate = head_sha.strip()
            msg = (
                f"head_sha {head_sha!r} (length={len(candidate)}) is not a full {SHA_FULL_LEN}-char hex "
                f"commit SHA — a review-evidence artifact binds to the exact reviewed tree. Pass the full "
                f"40-char SHA (e.g. `git rev-parse HEAD`)"
            )
            raise ReviewEvidenceError(msg)

        normalized_repos = _normalize_repos(repos)
        if normalized_kind == cls.Kind.INTEGRATION_REVIEW and len(normalized_repos) < _MIN_INTEGRATION_REPOS:
            msg = (
                f"an integration review must cover ≥ {_MIN_INTEGRATION_REPOS} distinct repos (got "
                f"{normalized_repos}) — it certifies the COMBINED cross-repo changeset"
            )
            raise ReviewEvidenceError(msg)

        with transaction.atomic():
            return cls.objects.create(
                ticket=ticket,
                kind=normalized_kind,
                reviewer_identity=reviewer,
                verdict=cleaned_verdict,
                head_sha=head_sha.strip().lower(),
                repos=normalized_repos,
            )
