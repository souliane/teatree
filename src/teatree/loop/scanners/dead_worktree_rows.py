"""Release only worktree registry rows whose checkout is proven dead."""

from dataclasses import dataclass

from django.db import transaction
from django.utils import timezone

from teatree.config import worktree_root
from teatree.core.factory.health_signal import HealthSignal
from teatree.core.models.known_issue import KnownIssue
from teatree.core.worktree.dead_row_release import release_dead_rows
from teatree.loop.scanners.base import ScanSignal

_SUMMARY_LIMIT = 500


@dataclass(slots=True)
class DeadWorktreeRowScanner:
    name: str = "dead_worktree_rows"

    @staticmethod
    def scan() -> list[ScanSignal]:
        outcome = release_dead_rows(worktree_root(), dry_run=False)
        with transaction.atomic():
            live: set[str] = set()
            kept: dict[str, list[str]] = {}
            for verdict in outcome.verdicts:
                if verdict.releasable:
                    continue
                kept.setdefault(str(verdict.disposition), []).append(
                    f"{verdict.worktree_pk} for {verdict.branch}: {verdict.reason}"
                )
            for disposition, rows in kept.items():
                fingerprint = f"dead-worktree:{disposition}"
                live.add(fingerprint)
                summary = f"{len(rows)} dead worktree row(s) kept ({disposition}): {'; '.join(rows)}"
                if len(summary) > _SUMMARY_LIMIT:
                    summary = summary[: _SUMMARY_LIMIT - 1] + "…"
                row = KnownIssue.objects.record_signal(
                    HealthSignal(
                        fingerprint=fingerprint,
                        severity=KnownIssue.Severity.WARNING,
                        kind="dead_worktree_row",
                        summary=summary,
                    )
                )
                if row.auto_resolve:
                    KnownIssue.objects.filter(pk=row.pk).update(auto_resolve=False)
            KnownIssue.objects.filter(
                kind="dead_worktree_row", source=KnownIssue.Source.AUTO, resolved_at__isnull=True
            ).exclude(fingerprint__in=live).update(resolved_at=timezone.now())
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
