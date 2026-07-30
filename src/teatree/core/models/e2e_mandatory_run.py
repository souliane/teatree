"""SHA-bound, POSTED green-E2E evidence artifact for the mandatory-E2E gate (#1967).

The mandatory-E2E gate is satisfied by green E2E evidence that is both bound to
the exact reviewed tree AND **posted** — a recorded-but-unposted run does NOT
satisfy the gate (user directive: "recorded e2e evidence is NOT enough — it must
be posted too"). ``E2eMandatoryRun`` is that durable record: one row per
``(ticket, head_sha, spec)``, carrying the run ``result`` and the ``posted_url``
of the SHA-bound ``e2e post-test-plan`` ticket comment.

The gate reads it via :meth:`has_green_evidence`, true only when a green run with
a non-empty ``posted_url`` exists for the ticket at the given SHA — a green run
at an earlier commit does NOT carry to a later tree, and a green run with no
posted comment does NOT satisfy the gate (the same SHA-binding ``MergeClear``
uses for its CLEAR, plus the posted-proof requirement).

Re-recording the same ``(ticket, head_sha, spec)`` updates the existing row
rather than appending a duplicate, so a red→green (or unposted→posted) rerun of
the same spec at the same tree leaves a single, current artifact (idempotent).
"""

from typing import ClassVar

from django.db import models, transaction
from django.utils import timezone

from teatree.core.models.ticket import Ticket

GREEN_RESULT = "green"


def _canonical_sha(head_sha: str) -> str:
    return head_sha.strip().lower()


class E2eMandatoryRun(models.Model):
    """One recorded E2E run for a ticket at a reviewed tree (#1967)."""

    class Result(models.TextChoices):
        GREEN = "green", "Green"
        RED = "red", "Red"

    ticket = models.ForeignKey(
        Ticket,
        on_delete=models.CASCADE,
        related_name="e2e_mandatory_runs",
    )
    head_sha = models.CharField(max_length=64)
    spec = models.CharField(max_length=512)
    result = models.CharField(max_length=16, choices=Result.choices)
    # The URL of the posted ``e2e post-test-plan`` ticket comment for this run.
    # Empty means recorded-but-unposted: the run does NOT satisfy the gate
    # (#1967 — posted proof is required, a local record is not enough).
    posted_url = models.CharField(max_length=512, blank=True, default="")
    recorded_at = models.DateTimeField(default=timezone.now)

    class Meta:
        db_table = "teatree_e2e_mandatory_run"
        ordering: ClassVar = ["-recorded_at"]
        constraints: ClassVar = [
            models.UniqueConstraint(
                fields=["ticket", "head_sha", "spec"],
                name="uniq_e2e_mandatory_run_ticket_sha_spec",
            )
        ]

    def __str__(self) -> str:
        return f"e2e-run<ticket={self.ticket_id}@{self.head_sha[:8]} {self.spec}={self.result}>"  # ty: ignore[unresolved-attribute]

    @classmethod
    def record(
        cls, *, ticket: Ticket, head_sha: str, spec: str, result: str, posted_url: str = ""
    ) -> "E2eMandatoryRun":
        """Record (or update) the E2E run for ``(ticket, head_sha, spec)``.

        Idempotent on the natural key: a rerun of the same spec at the same
        tree updates the existing row's result, ``posted_url`` and timestamp
        rather than appending a duplicate, so the artifact set always reflects
        the latest run per spec per tree. A green run with an empty
        ``posted_url`` is recorded but does NOT satisfy the gate.
        """
        clean_sha = _canonical_sha(head_sha)
        normalized = result.strip().lower()
        with transaction.atomic():
            row, _created = cls.objects.update_or_create(
                ticket=ticket,
                head_sha=clean_sha,
                spec=spec.strip(),
                defaults={"result": normalized, "posted_url": posted_url.strip(), "recorded_at": timezone.now()},
            )
            return row

    @classmethod
    def has_green_evidence(cls, ticket: Ticket, head_sha: str) -> bool:
        """True iff a green AND POSTED E2E run is recorded for *ticket* at exactly *head_sha*.

        A green run with no ``posted_url`` does NOT satisfy the gate — the
        evidence must be posted (the SHA-bound ``e2e post-test-plan`` comment),
        not merely recorded locally (#1967).
        """
        return (
            cls.objects.filter(ticket=ticket, head_sha=_canonical_sha(head_sha), result=GREEN_RESULT)
            .exclude(posted_url="")
            .exists()
        )

    @classmethod
    def has_visual_verification(cls, ticket: Ticket) -> bool:
        """True iff *ticket* has any green AND POSTED E2E run — the visual attestation.

        The snapshot-baseline gate runs at commit time, when the tree the new
        baseline lands on has no SHA yet, so it cannot bind to a specific
        ``head_sha`` the way :meth:`has_green_evidence` does. It instead reads
        this per-ticket signal: a green run whose evidence was POSTED proves the
        rendered result was verified and shown, which is exactly the
        visual-verification attestation a baseline change needs. A green run
        with no ``posted_url`` (recorded-but-unposted) does not count — the
        verification must have been posted, matching the mandatory-E2E gate's
        posted-proof rule.
        """
        return cls.objects.filter(ticket=ticket, result=GREEN_RESULT).exclude(posted_url="").exists()
