"""Durable operational-health issue registry (PR-17, M6).

A :class:`KnownIssue` row is the compaction-surviving record of one thing the
global-health aggregator considers wrong right now — a stale loop tick, a
scanner failing repeatedly, an overlay-declared problem. Rows are AUTO-derived
each health computation from the deterministic signals
(:mod:`teatree.core.factory.operational_health`) and reconciled against the live signal
set: a signal that clears auto-resolves its row by construction, so an operator
never chases a stale entry. Rows are also manually addable (an operator records
something the deterministic signals cannot see) and manually dismissable (an
operator acknowledges an auto-derived issue they choose to live with).

The dedupe key is ``fingerprint`` — one stable string per distinct problem. An
auto signal re-appearing on the next tick updates the existing row's
``last_seen`` rather than creating a duplicate; that same fingerprint is what a
persistent-issue ticket-filing path would dedupe against.

Severity is the input to the chip color: ``critical`` drives red on its own,
``warning`` drives yellow (and red only when three or more pile up) — the
thresholds live in :mod:`teatree.core.factory.operational_health`, this model only
carries the per-issue severity.
"""

from typing import TYPE_CHECKING, ClassVar

from django.db import models
from django.utils import timezone

if TYPE_CHECKING:
    from teatree.core.factory.operational_health import HealthSignal


class KnownIssueManager(models.Manager["KnownIssue"]):
    """Read + reconcile surface for the operational-health issue registry.

    Callers ask for the open set (``open()``), record a live signal
    (``record_signal``), reconcile the auto-derived rows against the current
    signal set (``reconcile``), or perform the two operator verbs
    (``add_manual`` / ``dismiss``). The manager owns the open predicate and the
    auto-resolve rule so no caller re-derives them.
    """

    def open(self) -> models.QuerySet["KnownIssue"]:
        """Every unresolved, un-dismissed row (the set the chip counts)."""
        return self.filter(resolved_at__isnull=True, dismissed_at__isnull=True)

    def record_signal(self, signal: "HealthSignal") -> "KnownIssue":
        """Upsert the auto-derived row for *signal*, keyed on its fingerprint.

        A first sighting creates the row; a repeat sighting refreshes
        ``last_seen``, ``severity``, ``summary`` and ``evidence_url`` (the
        signal is the source of truth for those) and re-opens a row that had
        auto-resolved since. A row an operator has dismissed stays dismissed —
        the sighting only refreshes ``last_seen`` so the reconcile pass still
        sees it as live and does not fight the dismissal.
        """
        now = timezone.now()
        row, created = self.get_or_create(
            fingerprint=signal.fingerprint,
            defaults={
                "overlay": signal.overlay,
                "kind": signal.kind,
                "severity": signal.severity,
                "summary": signal.summary,
                "evidence_url": signal.evidence_url,
                "source": KnownIssue.Source.AUTO,
                "first_seen": now,
                "last_seen": now,
            },
        )
        if created:
            return row
        row.severity = KnownIssue.Severity(signal.severity)
        row.summary = signal.summary
        row.evidence_url = signal.evidence_url
        row.kind = signal.kind
        row.overlay = signal.overlay
        row.last_seen = now
        if row.dismissed_at is None:
            row.resolved_at = None
        row.save(
            update_fields=[
                "severity",
                "summary",
                "evidence_url",
                "kind",
                "overlay",
                "last_seen",
                "resolved_at",
            ],
        )
        return row

    def reconcile(self, live_fingerprints: set[str]) -> int:
        """Auto-resolve every auto-derived row whose signal is no longer live.

        A row with ``source == AUTO`` and ``auto_resolve`` set whose fingerprint
        is absent from *live_fingerprints* has its signal cleared — the problem
        it recorded is gone, so it resolves by construction (this is the
        ``auto_resolve`` predicate the spec names). Manual rows and rows with
        ``auto_resolve`` cleared are never touched here. Returns the count
        resolved so the caller can log it.
        """
        stale = self.filter(
            source=KnownIssue.Source.AUTO,
            auto_resolve=True,
            resolved_at__isnull=True,
        ).exclude(fingerprint__in=live_fingerprints)
        return stale.update(resolved_at=timezone.now())

    def add_manual(self, text: str, *, severity: str = "", overlay: str = "") -> "KnownIssue":
        """Record an operator-authored issue the deterministic signals cannot see.

        A manual row never auto-resolves (``auto_resolve`` is cleared) — only an
        explicit ``dismiss`` closes it. Its fingerprint is unique per row so two
        manual entries with the same text stay distinct.
        """
        now = timezone.now()
        return self.create(
            fingerprint=f"manual:{now.timestamp()}:{text[:64]}",
            summary=text,
            severity=severity or KnownIssue.Severity.WARNING,
            overlay=overlay,
            source=KnownIssue.Source.MANUAL,
            auto_resolve=False,
            first_seen=now,
            last_seen=now,
        )

    def dismiss(self, issue_id: int) -> bool:
        """Acknowledge an open issue by pk — mark it dismissed. False when absent."""
        updated = self.filter(pk=issue_id, dismissed_at__isnull=True).update(dismissed_at=timezone.now())
        return updated > 0


class KnownIssue(models.Model):
    """One durable operational-health issue (auto-derived or operator-added)."""

    class Severity(models.TextChoices):
        CRITICAL = "critical", "Critical"
        WARNING = "warning", "Warning"

    class Source(models.TextChoices):
        AUTO = "auto", "Auto-derived"
        MANUAL = "manual", "Manual"

    fingerprint = models.CharField(max_length=255, unique=True)
    overlay = models.CharField(max_length=100, blank=True, default="")
    kind = models.CharField(max_length=64, blank=True, default="")
    severity = models.CharField(max_length=16, choices=Severity.choices, default=Severity.WARNING)
    summary = models.CharField(max_length=500)
    evidence_url = models.CharField(max_length=1000, blank=True, default="")
    source = models.CharField(max_length=8, choices=Source.choices, default=Source.AUTO)
    auto_resolve = models.BooleanField(default=True)
    first_seen = models.DateTimeField(default=timezone.now)
    last_seen = models.DateTimeField(default=timezone.now)
    resolved_at = models.DateTimeField(null=True, blank=True)
    dismissed_at = models.DateTimeField(null=True, blank=True)

    objects: ClassVar[KnownIssueManager] = KnownIssueManager()

    class Meta:
        db_table = "teatree_known_issue"
        ordering: ClassVar = ["severity", "first_seen"]

    def __str__(self) -> str:
        return f"known-issue<{self.severity}:{self.fingerprint}>"

    @property
    def is_open(self) -> bool:
        """True while the issue is neither resolved nor dismissed."""
        return self.resolved_at is None and self.dismissed_at is None
