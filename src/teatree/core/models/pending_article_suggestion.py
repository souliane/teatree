"""Per-article approval gate for scanner-ingested third-party prose (#1391).

The ``scanning-news`` skill (and any sibling RSS / arxiv / vendor-blog
scanner) historically called ``gh issue create --label from-news-scan``
directly after triaging an edition. That auto-create pattern treats
"interesting article" as a strong signal to ticketize, and the user
(correctly) treats this as backlog pollution: most articles are noise.

This model is the durable buffer between **scanner triage** and
**ticket creation**: a scanner records one
:class:`PendingArticleSuggestion` per candidate article, the user reviews
the batch (via DM or ``t3 news pending``), and only the user-approved
suggestions become real GitHub issues — via ``t3 news approve <id>``.

The model mirrors the ``DeferredQuestion`` / ``OnBehalfApproval`` shape:

* guarded factory :meth:`PendingArticleSuggestion.record` is the only
    path that writes a row, and it refuses empty payloads (no silent drop);
* :meth:`approve` / :meth:`reject` atomically claim and stamp
    ``decided_at`` so the same row can never be acted on twice;
* ``url_hash`` is the idempotency key — a second scanner pass on the
    same URL is a no-op via :meth:`record_if_new`.

Unlike ``OnBehalfApproval`` (which guards a *publication*), this row
guards a *backlog ingestion*. The shape is the same — durable,
single-use, scoped, audited — so the team can reason about all the
gates as one primitive family.
"""

import hashlib
from typing import ClassVar

from django.db import models, transaction
from django.utils import timezone


class PendingArticleSuggestionError(ValueError):
    """A :class:`PendingArticleSuggestion` was rejected at record time."""


class PendingArticleSuggestion(models.Model):
    """One candidate article queued for user approval before ticket creation.

    ``url_hash`` is the SHA-256 of the canonical article URL; a second
    scan that lands on the same URL is a no-op (the
    :meth:`record_if_new` factory enforces this via a unique-index
    short-circuit). ``source`` names the scanner that produced the row
    (e.g. ``"tldr-ai"``, ``"rundown-ai"``) for batch-level dedup and
    audit.
    """

    DECISION_PENDING = "pending"
    DECISION_APPROVED = "approved"
    DECISION_REJECTED = "rejected"

    created_at = models.DateTimeField(default=timezone.now)
    url = models.URLField(max_length=2048)
    url_hash = models.CharField(max_length=64, unique=True)
    title = models.TextField(blank=True, default="")
    summary = models.TextField()
    source = models.CharField(max_length=64, blank=True, default="")
    presented = models.BooleanField(default=False)
    presented_at = models.DateTimeField(null=True, blank=True)
    decision = models.CharField(
        max_length=16,
        choices=[
            (DECISION_PENDING, "pending"),
            (DECISION_APPROVED, "approved"),
            (DECISION_REJECTED, "rejected"),
        ],
        default=DECISION_PENDING,
    )
    decided_at = models.DateTimeField(null=True, blank=True)
    decider_id = models.CharField(max_length=255, blank=True, default="")
    decision_reason = models.TextField(blank=True, default="")
    created_ticket_url = models.URLField(max_length=2048, blank=True, default="")

    class Meta:
        db_table = "teatree_pending_article_suggestion"
        ordering: ClassVar = ["-created_at"]

    def __str__(self) -> str:
        title = self.title or self.url
        return f"pending-article<{self.pk}:{self.decision} '{title[:60]}'>"

    @property
    def is_pending(self) -> bool:
        return self.decision == self.DECISION_PENDING

    @staticmethod
    def hash_url(url: str) -> str:
        """Canonical idempotency key for a candidate article URL."""
        return hashlib.sha256(url.strip().encode("utf-8")).hexdigest()

    @classmethod
    def record(
        cls,
        *,
        url: str,
        summary: str,
        title: str = "",
        source: str = "",
    ) -> "PendingArticleSuggestion":
        """The single guarded factory; raises if URL/summary are empty.

        Construction is atomic so a rejected record leaves no partial row.
        Callers that want the idempotent "skip if already presented"
        behaviour should use :meth:`record_if_new` instead.
        """
        clean_url = url.strip()
        clean_summary = summary.strip()
        if not clean_url:
            msg = "url is required (#1391)"
            raise PendingArticleSuggestionError(msg)
        if not clean_summary:
            msg = "summary is required (#1391)"
            raise PendingArticleSuggestionError(msg)
        with transaction.atomic():
            return cls.objects.create(
                url=clean_url,
                url_hash=cls.hash_url(clean_url),
                title=title.strip(),
                summary=clean_summary,
                source=source.strip(),
            )

    @classmethod
    def record_if_new(
        cls,
        *,
        url: str,
        summary: str,
        title: str = "",
        source: str = "",
    ) -> "PendingArticleSuggestion | None":
        """Record a candidate, returning ``None`` if its URL is already queued.

        Idempotency on ``url_hash`` is the load-bearing invariant — a
        scanner that double-fires on the same edition must not produce
        duplicate suggestions. Returns the row on first record; returns
        ``None`` when a row with the same ``url_hash`` already exists
        regardless of its decision state (a previously-rejected URL is
        not re-presented).
        """
        clean_url = url.strip()
        if not clean_url:
            return None
        url_hash = cls.hash_url(clean_url)
        if cls.objects.filter(url_hash=url_hash).exists():
            return None
        try:
            return cls.record(url=clean_url, summary=summary, title=title, source=source)
        except PendingArticleSuggestionError:
            return None

    @classmethod
    def pending(cls, *, using: str | None = None) -> models.QuerySet["PendingArticleSuggestion"]:
        """Return the undecided queue, oldest first."""
        manager = cls.objects.using(using) if using else cls.objects
        return manager.filter(decision=cls.DECISION_PENDING).order_by("created_at")

    @classmethod
    def mark_batch_presented(cls, ids: list[int]) -> int:
        """Stamp ``presented=True`` / ``presented_at`` on the listed rows.

        Called by the scanner after the batch DM has been sent so the
        next tick doesn't re-DM the same suggestions.
        """
        if not ids:
            return 0
        now = timezone.now()
        return cls.objects.filter(pk__in=ids, presented=False).update(
            presented=True,
            presented_at=now,
        )

    @classmethod
    def approve(
        cls,
        suggestion_id: int,
        *,
        decider_id: str = "",
        ticket_url: str = "",
        using: str | None = None,
    ) -> "PendingArticleSuggestion | None":
        """Atomically claim the row as APPROVED and stamp the ticket URL.

        Returns the consumed row (so the caller can chain the issue
        creation), or ``None`` when the suggestion is missing or already
        decided. The ``ticket_url`` is recorded on the suggestion so the
        audit trail shows which approval produced which issue.
        """
        return cls._consume(
            suggestion_id,
            decision=cls.DECISION_APPROVED,
            decider_id=decider_id,
            ticket_url=ticket_url,
            reason="",
            using=using,
        )

    @classmethod
    def reject(
        cls,
        suggestion_id: int,
        *,
        decider_id: str = "",
        reason: str = "",
        using: str | None = None,
    ) -> "PendingArticleSuggestion | None":
        """Atomically claim the row as REJECTED with an optional reason."""
        return cls._consume(
            suggestion_id,
            decision=cls.DECISION_REJECTED,
            decider_id=decider_id,
            ticket_url="",
            reason=reason,
            using=using,
        )

    @classmethod
    def _consume(  # noqa: PLR0913 — single chokepoint for all decision transitions; each kwarg is a documented audit field.
        cls,
        suggestion_id: int,
        *,
        decision: str,
        decider_id: str,
        ticket_url: str,
        reason: str,
        using: str | None,
    ) -> "PendingArticleSuggestion | None":
        manager = cls.objects.using(using) if using else cls.objects
        with transaction.atomic(using=using):
            row = manager.select_for_update().filter(pk=suggestion_id, decision=cls.DECISION_PENDING).first()
            if row is None:
                return None
            now = timezone.now()
            row.decision = decision
            row.decided_at = now
            row.decider_id = decider_id
            if ticket_url:
                row.created_ticket_url = ticket_url
            if reason:
                row.decision_reason = reason
            row.save(
                update_fields=[
                    "decision",
                    "decided_at",
                    "decider_id",
                    "created_ticket_url",
                    "decision_reason",
                ],
                using=using,
            )
            return row
