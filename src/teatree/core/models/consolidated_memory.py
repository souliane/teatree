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

Every rung has a writer, so the ladder reaches its terminal states and
:meth:`ConsolidatedMemoryManager.prunable` — the decay pass's
transfer-before-prune rail — is fed rather than structurally empty:
recording a cited cluster lands it VERIFIED, a later cluster that covers an
untriaged row's whole member set supersedes it
(:meth:`ConsolidatedMemoryManager.supersede_covered_by`), and the merged fix
of a core gap promotes the rule into that fix's home and expires the prose
(:meth:`ConsolidatedMemory.retire`). Only PROMOTED and EXPIRED count as a durable
home: a superseded rule's lesson lives in its replacement, which has to land first.

A CANDIDATE may not advance without a real cited mistake
(``verified_citation``) — an uncited rule is a hallucinated lesson and is
refused promotion. BINDING feedback is never silently dropped: expiring a
binding row raises :class:`BindingFeedbackError` rather than retiring it.

Mirrors the durable-gate family already in core —
:class:`teatree.core.models.pending_article_suggestion.PendingArticleSuggestion`
(idempotent sha256 key, TextChoices status ladder, model-owned
transitions, custom manager).
"""

from collections.abc import Mapping
from typing import TYPE_CHECKING, ClassVar

from django.db import models
from django.utils import timezone

if TYPE_CHECKING:
    from collections.abc import Sequence


class BindingFeedbackError(Exception):
    """Raised when a BINDING consolidated rule would be silently retired.

    BINDING feedback is load-bearing user doctrine; expiring it would drop
    it without a trace. The expire transition refuses a binding row and
    raises this instead so the caller must handle it explicitly.
    """


class ConsolidatedMemoryManager(models.Manager["ConsolidatedMemory"]):
    """Read surface for the consolidation engine and the index pruner."""

    def prunable(self) -> "models.QuerySet[ConsolidatedMemory]":
        """Rows whose landed lesson + recorded durable home let an index line be pruned."""
        return self.filter(status__in=ConsolidatedMemory.Status.homed()).exclude(durable_destination="")

    def verified_for_overlay(self, overlay: str) -> "models.QuerySet[ConsolidatedMemory]":
        """VERIFIED rows for *overlay* — distilled rules with a real cited mistake."""
        return self.filter(overlay=overlay, status=ConsolidatedMemory.Status.VERIFIED)

    def supersede_covered_by(self, row: "ConsolidatedMemory") -> list["ConsolidatedMemory"]:
        """Supersede every untriaged row whose members *row* wholly covers, returning them.

        A re-clustered lesson gains a member and so a NEW ``cluster_key`` (sha256 over
        the member paths), leaving the narrower row queued for triage beside the wider
        one that carries the same lesson plus more. The narrow row is retired from the
        queue and pointed at its replacement. Superseding is not a durable home — the
        replacement still has to land before :meth:`prunable` lets decay act on either.
        A strict superset only: a partial overlap is a different cluster. A row Pass 2
        already routed keeps its disposition decision, and a BINDING row is never
        absorbed (Decision-3, the merge phase's rule for two binding near-duplicates).
        """
        live = (
            self.exclude(pk=row.pk)
            .filter(overlay=row.overlay, is_binding=False, disposition=ConsolidatedMemory.Disposition.UNTRIAGED)
            .exclude(status__in=ConsolidatedMemory.Status.terminal())
        )
        covered = [other for other in live if other.member_paths and other.member_paths < row.member_paths]
        for other in covered:
            other.supersede(row)
        return covered

    def schema_count(self, overlay: str) -> int:
        """Count of all consolidation rows recorded for *overlay*."""
        return self.filter(overlay=overlay).count()

    def untriaged(self) -> "models.QuerySet[ConsolidatedMemory]":
        """Rows the Pass-2 promote pass has not yet classified (the queue to drain)."""
        return self.filter(disposition=ConsolidatedMemory.Disposition.UNTRIAGED)

    def needs_ticket(self) -> "models.QuerySet[ConsolidatedMemory]":
        """Core-gap rows classified but not yet promoted to a fix — the drain queue.

        A row Pass 2 moved to ``CORE_GAP_NEEDS_TICKET`` that still carries NO tracking
        ticket (``ticket_url``): the gap was detected but its fix was never driven onto
        the umbrella. The promote pass drains these alongside :meth:`untriaged` so a
        core gap a prior run classified — e.g. one an old dry-run stranded — is never
        left permanently un-promoted, silently detected but never fixed.
        """
        return self.filter(
            disposition=ConsolidatedMemory.Disposition.CORE_GAP_NEEDS_TICKET,
            ticket_url="",
        )

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

        @classmethod
        def terminal(cls) -> frozenset["ConsolidatedMemory.Status"]:
            """The rungs a rule stops on — landed in a durable home, replaced, or retired."""
            return frozenset({cls.PROMOTED, cls.SUPERSEDED, cls.EXPIRED})

        @classmethod
        def homed(cls) -> frozenset["ConsolidatedMemory.Status"]:
            """The terminal rungs whose lesson has landed somewhere durable; a replaced rule has not."""
            return frozenset({cls.PROMOTED, cls.EXPIRED})

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
        source_files: "Sequence[object]",
        member_count: int,
        max_member_weight: int,
        is_binding: bool,
        overlay: str = "",
        durable_destination: str = "",
        verified_citation: str = "",
    ) -> "ConsolidatedMemory":
        """Idempotently record one cluster keyed on ``cluster_key``.

        A re-run that re-clusters the same members (same ``cluster_key``)
        returns the existing row untouched rather than distilling a
        duplicate. A cited cluster is recorded VERIFIED — the caller's citation
        was already proven present in a cited snippet, so a second trust step
        would assert nothing; an uncited one is a CANDIDATE until
        :meth:`mark_verified` supplies the mistake it rests on.
        """
        cited = verified_citation.strip()
        row, _ = cls.objects.get_or_create(
            cluster_key=cluster_key,
            defaults={
                "rule": rule,
                "source_files": source_files,
                "member_count": member_count,
                "max_member_weight": max_member_weight,
                "is_binding": is_binding,
                "overlay": overlay,
                "durable_destination": durable_destination,
                "verified_citation": cited,
                "status": cls.Status.VERIFIED if cited else cls.Status.CANDIDATE,
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

    def reopen_core_gap(self) -> None:
        """TICKETED → CORE_GAP_NEEDS_TICKET, so the next pass re-offers a gap its fix dropped."""
        if self.disposition != self.Disposition.TICKETED:
            msg = f"reopen_core_gap requires a TICKETED row, not {self.disposition!r}"
            raise ValueError(msg)
        self.disposition = self.Disposition.CORE_GAP_NEEDS_TICKET
        self.ticket_url = ""
        self.save(update_fields=["disposition", "ticket_url", "updated_at"])

    def retire(self, archive_path: str) -> None:
        """TICKETED → RESOLVED_RETIRED, archiving the prose now its fix has landed.

        The end of the drain: the gap the memory confessed is closed in code, so the
        rule is PROMOTED into the home that fix landed in and the prose EXPIRED
        (archived, never silently dropped) — the terminal pair
        :meth:`ConsolidatedMemoryManager.prunable` reads, so decay may finally age out
        the memories this row homes. Refuses a BINDING row — binding feedback is
        load-bearing user doctrine, raising :class:`BindingFeedbackError` rather than
        retiring it.
        """
        if self.is_binding:
            msg = f"refusing to retire BINDING consolidated rule {self.pk} — binding feedback is never dropped"
            raise BindingFeedbackError(msg)
        self.mark_promoted(self.durable_destination)
        self.expire(archive_path)
        self.disposition = self.Disposition.RESOLVED_RETIRED
        self.save(update_fields=["disposition", "updated_at"])

    @property
    def member_paths(self) -> frozenset[str]:
        """The member path strings behind this cluster, whatever shape they were stored in.

        A member is a bare path string (what the engine writes) or a ``{"path": ...}``
        object (older / hand-written rows); anything else is not a member.
        """
        if not isinstance(self.source_files, list):
            return frozenset()
        paths = {member for member in self.source_files if isinstance(member, str)}
        paths.update(
            str(member["path"])
            for member in self.source_files
            if isinstance(member, Mapping) and isinstance(member.get("path"), str)
        )
        return frozenset(paths)

    @property
    def can_prune_index_line(self) -> bool:
        """True iff this row's lesson has landed and its durable home is recorded.

        The index pruner removes a MEMORY.md index line only once the rule has
        landed (promoted or expired) AND its durable destination is recorded —
        otherwise pruning the line would orphan the rule with no recoverable home.
        A superseded rule has not landed; its replacement carries the lesson.
        """
        return self.status in self.Status.homed() and bool(self.durable_destination)
