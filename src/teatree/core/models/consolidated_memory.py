"""Consolidation ledger for idle-time memory consolidation (#1933).

The dreaming engine clusters related feedback/lesson members surfaced
during sessions and distils each cluster into one imperative rule. The DB
row is the ledger of record — the canonical state, audit trail, and
idempotency anchor; the rendered durable home (a topic file) is the
output the rule ultimately lands in. Splitting the two means a re-run
that re-clusters the same members finds the existing row (idempotent on
``cluster_key``) instead of distilling a duplicate, and the status ladder
tracks each cluster from a raw CANDIDATE through a cited VERIFIED rule, a
PROMOTED durable line, and finally SUPERSEDED or EXPIRED retirement.

A CANDIDATE may not advance without a real cited mistake
(``verified_citation``) — an uncited rule is a hallucinated lesson and is
refused promotion. BINDING feedback is never silently dropped: expiring a
binding row raises :class:`BindingFeedbackError` rather than retiring it.

Mirrors the durable-gate family already in core —
:class:`teatree.core.models.pending_article_suggestion.PendingArticleSuggestion`
(idempotent sha256 key, TextChoices status ladder, model-owned
transitions, custom manager).
"""

from typing import ClassVar

from django.db import models
from django.utils import timezone


class BindingFeedbackError(Exception):
    """Raised when a BINDING consolidated rule would be silently retired.

    BINDING feedback is load-bearing user doctrine; expiring it would drop
    it without a trace. The expire transition refuses a binding row and
    raises this instead so the caller must handle it explicitly.
    """


class ConsolidatedMemoryManager(models.Manager["ConsolidatedMemory"]):
    """Read surface for the consolidation engine and the index pruner."""

    def prunable(self) -> "models.QuerySet[ConsolidatedMemory]":
        """Rows whose terminal status + recorded durable home let an index line be pruned."""
        return self.filter(
            status__in=[
                ConsolidatedMemory.Status.PROMOTED,
                ConsolidatedMemory.Status.SUPERSEDED,
                ConsolidatedMemory.Status.EXPIRED,
            ],
        ).exclude(durable_destination="")

    def verified_for_overlay(self, overlay: str) -> "models.QuerySet[ConsolidatedMemory]":
        """VERIFIED rows for *overlay* — distilled rules with a real cited mistake."""
        return self.filter(overlay=overlay, status=ConsolidatedMemory.Status.VERIFIED)

    def schema_count(self, overlay: str) -> int:
        """Count of all consolidation rows recorded for *overlay*."""
        return self.filter(overlay=overlay).count()

    def untriaged(self) -> "models.QuerySet[ConsolidatedMemory]":
        """Rows the Pass-2 promote pass has not yet classified (the queue to drain)."""
        return self.filter(disposition=ConsolidatedMemory.Disposition.UNTRIAGED)

    def awaiting_ticket_close(self) -> "models.QuerySet[ConsolidatedMemory]":
        """TICKETED rows whose linked teatree ticket may now be closed → retirable."""
        return self.filter(disposition=ConsolidatedMemory.Disposition.TICKETED).exclude(ticket_url="")


class ConsolidatedMemory(models.Model):
    """One distilled rule per member cluster — the consolidation ledger row.

    ``cluster_key`` (sha256 over the normalized member identities) is the
    unique idempotency anchor: re-clustering the same members on a later
    dream run finds the existing row via :meth:`record_cluster` instead of
    distilling a duplicate. The status ladder is monotonic forward;
    transitions live on the model so callers never hand-stamp a status.
    """

    class Status(models.TextChoices):
        CANDIDATE = "candidate", "Candidate"
        VERIFIED = "verified", "Verified"
        PROMOTED = "promoted", "Promoted"
        SUPERSEDED = "superseded", "Superseded"
        EXPIRED = "expired", "Expired"

    class Disposition(models.TextChoices):
        """Pass-2 (#2426) draining queue: where a consolidated rule's lesson belongs.

        The ledger is a queue that DRAINS, not a pile that grows. ``UNTRIAGED`` is a
        freshly-recorded row Pass 2 has not classified yet. ``USER_SPECIFIC_KEEP``
        legitimately stays as memory (tone, local paths, per-user workflow).
        ``CORE_GAP_NEEDS_TICKET`` is a generic/teatree-core lesson — a confession of
        a workflow gap that must be fixed in code; ``TICKETED`` once a tracking issue
        is filed (``ticket_url`` recorded); ``RESOLVED_RETIRED`` once the fix lands and
        the memory is archived.
        """

        UNTRIAGED = "untriaged", "Untriaged"
        USER_SPECIFIC_KEEP = "user_specific_keep", "User-specific (keep as memory)"
        CORE_GAP_NEEDS_TICKET = "core_gap_needs_ticket", "Core gap (needs ticket)"
        TICKETED = "ticketed", "Ticketed"
        RESOLVED_RETIRED = "resolved_retired", "Resolved (retired)"

    cluster_key = models.CharField(max_length=64, unique=True)
    status = models.CharField(max_length=16, choices=Status.choices, default=Status.CANDIDATE)
    disposition = models.CharField(max_length=24, choices=Disposition.choices, default=Disposition.UNTRIAGED)
    rule = models.TextField()
    source_files = models.JSONField(default=list)
    durable_destination = models.CharField(max_length=512, blank=True, default="")
    ticket_url = models.CharField(max_length=512, blank=True, default="")
    is_binding = models.BooleanField(default=False)
    member_count = models.PositiveIntegerField()
    max_member_weight = models.PositiveIntegerField()
    verified_citation = models.TextField(blank=True, default="")
    archive_path = models.CharField(max_length=512, blank=True, default="")
    superseded_by = models.ForeignKey(
        "self",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="supersedes",
    )
    overlay = models.CharField(max_length=64, blank=True, default="")
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)
    promoted_at = models.DateTimeField(null=True, blank=True)
    expired_at = models.DateTimeField(null=True, blank=True)

    objects: ClassVar[ConsolidatedMemoryManager] = ConsolidatedMemoryManager()

    class Meta:
        db_table = "teatree_consolidated_memory"
        ordering: ClassVar = ["-created_at"]

    def __str__(self) -> str:
        return f"consolidated-memory<{self.pk}:{self.status}:{self.rule[:40]}>"

    @classmethod
    # ast-grep-ignore: ac-django-no-complexity-suppressions
    def record_cluster(  # noqa: PLR0913 — guarded ledger factory: each kwarg is a documented column, kwargs-only.
        cls,
        *,
        cluster_key: str,
        rule: str,
        source_files: list[object],
        member_count: int,
        max_member_weight: int,
        is_binding: bool,
        overlay: str = "",
    ) -> "ConsolidatedMemory":
        """Idempotently record one cluster keyed on ``cluster_key``.

        A re-run that re-clusters the same members (same ``cluster_key``)
        returns the existing row untouched rather than distilling a
        duplicate. The row is created as a CANDIDATE on first record.
        """
        row, _ = cls.objects.get_or_create(
            cluster_key=cluster_key,
            defaults={
                "rule": rule,
                "source_files": source_files,
                "member_count": member_count,
                "max_member_weight": max_member_weight,
                "is_binding": is_binding,
                "overlay": overlay,
            },
        )
        return row

    def mark_verified(self, citation: str) -> None:
        """CANDIDATE → VERIFIED, recording the real cited mistake.

        Refuses an empty citation: a rule with no cited mistake is a
        hallucinated lesson and may never leave CANDIDATE.
        """
        cited = citation.strip()
        if not cited:
            msg = "mark_verified requires a non-empty citation — an uncited rule cannot leave CANDIDATE"
            raise ValueError(msg)
        self.status = self.Status.VERIFIED
        self.verified_citation = cited
        self.save(update_fields=["status", "verified_citation", "updated_at"])

    def mark_promoted(self, destination: str) -> None:
        """→ PROMOTED, recording the durable home the rule landed in."""
        self.status = self.Status.PROMOTED
        self.durable_destination = destination.strip()
        self.promoted_at = timezone.now()
        self.save(update_fields=["status", "durable_destination", "promoted_at", "updated_at"])

    def supersede(self, by: "ConsolidatedMemory") -> None:
        """→ SUPERSEDED, pointing at the row that replaces this one."""
        self.status = self.Status.SUPERSEDED
        self.superseded_by = by
        self.save(update_fields=["status", "superseded_by", "updated_at"])

    def expire(self, archive_path: str) -> None:
        """→ EXPIRED, recording where the retired rule was archived.

        Refuses a BINDING row: binding feedback is load-bearing user
        doctrine and is never silently dropped — raises
        :class:`BindingFeedbackError` instead.
        """
        if self.is_binding:
            msg = f"refusing to expire BINDING consolidated rule {self.pk} — binding feedback is never dropped"
            raise BindingFeedbackError(msg)
        self.status = self.Status.EXPIRED
        self.archive_path = archive_path.strip()
        self.expired_at = timezone.now()
        self.save(update_fields=["status", "archive_path", "expired_at", "updated_at"])

    def classify_user_specific(self) -> None:
        """Pass-2 triage → USER_SPECIFIC_KEEP: the lesson legitimately stays as memory.

        Personal tone, local paths, per-user workflow — teatree cannot encode these,
        so the row stays a memory safety net and is removed from the triage queue.
        """
        self.disposition = self.Disposition.USER_SPECIFIC_KEEP
        self.save(update_fields=["disposition", "updated_at"])

    def classify_core_gap(self) -> None:
        """Pass-2 triage → CORE_GAP_NEEDS_TICKET: a generic lesson that must be fixed in code.

        The row is a confession that teatree core has a workflow gap. Marking it
        queues it for ticket-filing; the prose is retired once that fix lands.
        """
        self.disposition = self.Disposition.CORE_GAP_NEEDS_TICKET
        self.save(update_fields=["disposition", "updated_at"])

    def mark_ticketed(self, ticket_url: str) -> None:
        """CORE_GAP_NEEDS_TICKET → TICKETED, recording the tracking issue's URL.

        Refuses an empty URL: a ticketed disposition with no back-reference would
        orphan the row with no way to retire it on the fix landing.
        """
        url = ticket_url.strip()
        if not url:
            msg = "mark_ticketed requires a non-empty ticket URL — a ticketed row needs a back-reference"
            raise ValueError(msg)
        self.disposition = self.Disposition.TICKETED
        self.ticket_url = url
        self.save(update_fields=["disposition", "ticket_url", "updated_at"])

    def retire(self, archive_path: str) -> None:
        """TICKETED → RESOLVED_RETIRED, archiving the prose now its fix has landed.

        The end of the drain: the gap the memory confessed is closed in code, so the
        prose is retired (archived, never silently dropped). Refuses a BINDING row —
        binding feedback is load-bearing user doctrine, raising
        :class:`BindingFeedbackError` rather than retiring it.
        """
        if self.is_binding:
            msg = f"refusing to retire BINDING consolidated rule {self.pk} — binding feedback is never dropped"
            raise BindingFeedbackError(msg)
        self.disposition = self.Disposition.RESOLVED_RETIRED
        self.archive_path = archive_path.strip()
        self.expired_at = timezone.now()
        self.save(update_fields=["disposition", "archive_path", "expired_at", "updated_at"])

    @property
    def can_prune_index_line(self) -> bool:
        """True iff this row is terminal and has a recorded durable home.

        The index pruner removes a MEMORY.md index line only once the rule
        has reached a terminal status (promoted/superseded/expired) AND its
        durable destination is recorded — otherwise pruning the line would
        orphan the rule with no recoverable home.
        """
        terminal = {self.Status.PROMOTED, self.Status.SUPERSEDED, self.Status.EXPIRED}
        return self.status in terminal and bool(self.durable_destination)
