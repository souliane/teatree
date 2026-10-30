"""Release only worktree registry rows whose checkout is proven dead."""

from dataclasses import dataclass
from datetime import timedelta

from django.db import transaction
from django.utils import timezone

from teatree.config import worktree_root
from teatree.core.factory.health_signal import HealthSignal
from teatree.core.models.known_issue import KnownIssue
from teatree.core.models.resource_pressure_marker import ResourcePressureMarker
from teatree.core.worktree.dead_row_release import release_dead_rows
from teatree.loop.scanners.base import ScanSignal


@dataclass(slots=True)
class DeadWorktreeRowScanner:
    cadence_minutes: int = 30
    name: str = "dead_worktree_rows"

    def scan(self) -> list[ScanSignal]:
        marker = ResourcePressureMarker.load()
        now = timezone.now()
        if marker.last_dead_worktree_scan_at and now - marker.last_dead_worktree_scan_at < timedelta(
            minutes=self.cadence_minutes
        ):
            return []
        outcome = release_dead_rows(worktree_root(), dry_run=False)
        with transaction.atomic():
            live: set[str] = set()
            for verdict in outcome.verdicts:
                if verdict.releasable:
                    continue
                fingerprint = f"dead-worktree:{verdict.worktree_pk}:{verdict.disposition}"
                live.add(fingerprint)
                row = KnownIssue.objects.record_signal(
                    HealthSignal(
                        fingerprint=fingerprint,
                        severity=KnownIssue.Severity.WARNING,
                        kind="dead_worktree_row",
                        summary=f"worktree row {verdict.worktree_pk} for {verdict.branch} kept: {verdict.reason}",
                    )
                )
                if row.auto_resolve:
                    KnownIssue.objects.filter(pk=row.pk).update(auto_resolve=False)
            KnownIssue.objects.filter(
                kind="dead_worktree_row", source=KnownIssue.Source.AUTO, resolved_at__isnull=True
            ).exclude(fingerprint__in=live).update(resolved_at=timezone.now())
            marker.last_dead_worktree_scan_at = now
            marker.save(update_fields=["last_dead_worktree_scan_at"])
        released = outcome.released_pks
        if not released:
            return []
        return [
            ScanSignal(
                kind="dead_worktree_rows.released",
                summary=f"released {len(released)} dead worktree registry row(s)",
                payload={"released": sorted(released)},
            )
        ]
