"""Ask-gate candidate queue for the daily news scanner (#1391).

The ``scanning-news`` skill used to mass-create ``souliane/teatree``
issues via ``gh issue create --label from-news-scan`` after triaging
TLDR AI / Rundown AI editions — no per-article user approval. The
result was backlog pollution: most articles are noise, and copying them
in as issues confused "I read this" with "we should build this".

This model is the durable ask-gate. Instead of auto-filing, the skill
records one :class:`PendingArticleSuggestion` row per candidate article
(``PENDING``). The user reviews the batch and approves or rejects each
one; an issue is created **only** on approval. With no approval the row
stays ``PENDING`` and nothing is filed — default is no-op.

Idempotency is by source-URL hash: re-scanning the same article URL on
a later tick finds the existing row and does not enqueue a duplicate
candidate. This mirrors the durable-gate family already in core —
:class:`teatree.core.models.db_approval.DbApproval` (per-invocation DB
approval) and :class:`teatree.core.models.deferred_question.DeferredQuestion`
(away-mode question queue): a recorded, durable, reviewable row that
gates an otherwise-autonomous action behind explicit user confirmation.
"""

import hashlib
from typing import ClassVar

from django.db import models, transaction
from django.utils import timezone


class PendingArticleSuggestion(models.Model):
    """One candidate news article awaiting user approval to file as an issue.

    A row is created by the scanning-news skill for each article it
    triages as a possible t3 improvement. The default state is
    ``PENDING`` — nothing is filed until the user approves. ``url_hash``
    (sha256 of the source URL) is unique so a re-scan of the same
    article does not enqueue a duplicate candidate.
    """

    class Status(models.TextChoices):
        PENDING = "pending", "Pending"
        APPROVED = "approved", "Approved"
        REJECTED = "rejected", "Rejected"

    overlay = models.CharField(max_length=64, blank=True, default="")
    url = models.URLField(max_length=1024)
    url_hash = models.CharField(max_length=64, unique=True)
    title = models.CharField(max_length=512, blank=True, default="")
    summary = models.TextField(blank=True, default="")
    status = models.CharField(max_length=16, choices=Status.choices, default=Status.PENDING)
    issue_url = models.URLField(max_length=1024, blank=True, default="")
    created_at = models.DateTimeField(default=timezone.now)
    decided_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        db_table = "teatree_pending_article_suggestion"
        ordering: ClassVar = ["-created_at"]

    def __str__(self) -> str:
        return f"pending-article<{self.pk}:{self.status}:{self.title[:40]}>"

    @staticmethod
    def hash_url(url: str) -> str:
        """Stable sha256 hex digest of the normalized source URL."""
        return hashlib.sha256(url.strip().encode("utf-8")).hexdigest()

    @classmethod
    def record_candidate(
        cls,
        *,
        url: str,
        title: str = "",
        summary: str = "",
        overlay: str = "",
    ) -> "PendingArticleSuggestion | None":
        """Idempotently enqueue one PENDING candidate; return it or ``None`` on dup.

        ``None`` means a row for this exact source URL already exists (on
        any prior tick, in any state) — the candidate is not enqueued
        again, so a daily re-scan of the same article never spams the
        queue. The insert is atomic so a concurrent second scanner cannot
        double-write the same URL.
        """
        clean_url = url.strip()
        if not clean_url:
            return None
        digest = cls.hash_url(clean_url)
        with transaction.atomic():
            row, created = cls.objects.get_or_create(
                url_hash=digest,
                defaults={
                    "url": clean_url,
                    "title": title.strip(),
                    "summary": summary.strip(),
                    "overlay": overlay.strip(),
                },
            )
        return row if created else None

    def approve(self, *, issue_url: str = "") -> None:
        """Mark this candidate APPROVED — the user authorized filing the issue.

        ``issue_url`` records where the issue was filed (for the audit
        trail). Idempotent on the state stamp; ``decided_at`` is set once.
        """
        self.status = self.Status.APPROVED
        self.issue_url = issue_url.strip()
        self.decided_at = timezone.now()
        self.save(update_fields=["status", "issue_url", "decided_at"])

    def reject(self) -> None:
        """Mark this candidate REJECTED — no issue is filed for this article."""
        self.status = self.Status.REJECTED
        self.decided_at = timezone.now()
        self.save(update_fields=["status", "decided_at"])
