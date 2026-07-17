"""Ask-gate recommendation queue for the needs-triage assessor loop.

The ``triage_assessor`` mini-loop discovers OPEN ``needs-triage`` issues and has a
shell-denied agent assess each — returning a keep/close/needs_info verdict with a
rationale. The agent CANNOT act (it has no shell); it hands the batch back and the
recorder persists one :class:`PendingTriageRecommendation` row per issue
(``PENDING``). **Nothing acts autonomously.** The user reviews the batch and
approves or rejects each row via the interactive ``t3:triaging-issues`` skill,
which runs ``gh issue close/edit/comment`` only on approval.

Idempotency is by issue-URL hash: re-assessing the same issue URL on a later tick
finds the existing row and does not enqueue a duplicate. An unknown verdict fails
CLOSED — the row is dropped and logged, never stored with a bad verdict that the
approval skill could not act on. This mirrors the durable-gate family already in
core — :class:`teatree.core.models.pending_article_suggestion.PendingArticleSuggestion`
(the news-scan ask-gate) and
:class:`teatree.core.models.deferred_question.DeferredQuestion`: a recorded,
durable, reviewable row that gates an otherwise-autonomous action behind explicit
user confirmation.
"""

import hashlib
import logging
from typing import ClassVar

from django.db import models, transaction
from django.utils import timezone

logger = logging.getLogger(__name__)

#: The verdicts a triage assessment may carry. An assessment outside this set is
#: dropped fail-closed (the approval skill has no action for an unknown verdict).
VALID_TRIAGE_VERDICTS: frozenset[str] = frozenset({"keep", "close", "needs_info"})


class PendingTriageRecommendation(models.Model):
    """One assessed ``needs-triage`` issue awaiting user approval to act.

    A row is created by the triage-assessor recorder for each OPEN ``needs-triage``
    issue the assessor agent judged. The default state is ``PENDING`` — nothing is
    acted on until the user approves. ``url_hash`` (sha256 of the issue URL) is
    unique so a re-assessment of the same issue does not enqueue a duplicate.
    """

    class Status(models.TextChoices):
        PENDING = "pending", "Pending"
        APPROVED = "approved", "Approved"
        REJECTED = "rejected", "Rejected"

    overlay = models.CharField(max_length=64, blank=True, default="")
    issue_url = models.URLField(max_length=1024)
    url_hash = models.CharField(max_length=64, unique=True)
    title = models.CharField(max_length=512, blank=True, default="")
    verdict = models.CharField(max_length=16, blank=True, default="")
    suggested_labels = models.JSONField(default=list, blank=True)
    priority = models.CharField(max_length=32, blank=True, default="")
    duplicate_of = models.URLField(max_length=1024, blank=True, default="")
    rationale = models.TextField(blank=True, default="")
    status = models.CharField(max_length=16, choices=Status.choices, default=Status.PENDING)
    action_taken = models.TextField(blank=True, default="")
    created_at = models.DateTimeField(default=timezone.now)
    decided_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        db_table = "teatree_pending_triage_recommendation"
        ordering: ClassVar = ["-created_at"]

    def __str__(self) -> str:
        return f"pending-triage<{self.pk}:{self.status}:{self.verdict}:{self.title[:40]}>"

    @staticmethod
    def hash_url(url: str) -> str:
        """Stable sha256 hex digest of the normalized issue URL."""
        return hashlib.sha256(url.strip().encode("utf-8")).hexdigest()

    @classmethod
    # ast-grep-ignore: ac-django-no-complexity-suppressions
    def record_candidate(  # noqa: PLR0913 — guarded factory: each kwarg is a documented column, kwargs-only.
        cls,
        *,
        issue_url: str,
        verdict: str,
        title: str = "",
        suggested_labels: "list[str] | None" = None,
        priority: str = "",
        duplicate_of: str = "",
        rationale: str = "",
        overlay: str = "",
    ) -> "PendingTriageRecommendation | None":
        """Idempotently enqueue one PENDING recommendation; return it or ``None``.

        ``None`` means either the issue URL is blank, the ``verdict`` is not one of
        :data:`VALID_TRIAGE_VERDICTS` (dropped fail-closed and logged — the approval
        skill has no action for it), or a row for this exact issue URL already exists
        (on any prior tick, in any state) so a re-assessment never spams the queue.
        The insert is atomic so a concurrent second assessment cannot double-write.
        """
        clean_url = issue_url.strip()
        if not clean_url:
            return None
        clean_verdict = verdict.strip().lower()
        if clean_verdict not in VALID_TRIAGE_VERDICTS:
            logger.info("Dropping triage recommendation; unknown verdict %r for %s", verdict, clean_url)
            return None
        digest = cls.hash_url(clean_url)
        if cls.objects.filter(url_hash=digest).exists():
            return None
        with transaction.atomic():
            row, created = cls.objects.get_or_create(
                url_hash=digest,
                defaults={
                    "issue_url": clean_url,
                    "verdict": clean_verdict,
                    "title": title.strip(),
                    "suggested_labels": [s for s in (suggested_labels or []) if isinstance(s, str) and s],
                    "priority": priority.strip(),
                    "duplicate_of": duplicate_of.strip(),
                    "rationale": rationale.strip(),
                    "overlay": overlay.strip(),
                },
            )
        return row if created else None

    def approve(self, *, action_taken: str = "") -> None:
        """Mark this recommendation APPROVED — the user authorized acting on it.

        ``action_taken`` records what the approval skill did (e.g. ``closed via gh
        issue close``) for the audit trail. Idempotent on the state stamp;
        ``decided_at`` is set once.
        """
        self.status = self.Status.APPROVED
        self.action_taken = action_taken.strip()
        self.decided_at = timezone.now()
        self.save(update_fields=["status", "action_taken", "decided_at"])

    def reject(self) -> None:
        """Mark this recommendation REJECTED — no action is taken on the issue."""
        self.status = self.Status.REJECTED
        self.decided_at = timezone.now()
        self.save(update_fields=["status", "decided_at"])
