"""``ForgottenMergeDetector`` — a green/mergeable PR with a stale CLEAR.

Fires when a ``MergeClear`` row has been issued more than 30 minutes ago
and no ``MergeAudit`` row references it — i.e. the keystone merge
substrate has CLEARED the PR but the loop has not consumed the CLEAR.

Severity is ``error``: a stalled keystone merge means a green-CI PR is
sitting unmerged, which is the exact failure class § 17.4.4 attests
against.  The ladder ceiling is ``slack`` per the issue plan (statusline
→ slack); ``auto_fix`` is ``False`` (the loop merges via the keystone
transition, never raw ``gh pr merge`` — re-issuing the merge is a
loop-side action gated by independent review).
"""

import datetime as dt
from dataclasses import dataclass
from typing import ClassVar

from django.utils import timezone

from teatree.core.models.merge_clear import MergeClear
from teatree.loop.scanners.base import ScanSignal
from teatree.loop.self_improve.dedup import canonical_key, state_hash
from teatree.loop.self_improve.detectors.base import ActionRung, DetectorReport

DEFAULT_AGE_THRESHOLD = dt.timedelta(minutes=30)


@dataclass(slots=True)
class ForgottenMergeDetector:
    """A CLEAR older than 30 min with no matching MergeAudit row."""

    name: ClassVar[str] = "forgotten_merge"
    tier: ClassVar[str] = "cheap"
    severity: ClassVar[str] = "error"
    max_rung: ClassVar[str] = ActionRung.SLACK
    auto_fix: ClassVar[bool] = False

    age_threshold: dt.timedelta = DEFAULT_AGE_THRESHOLD

    def detect(self) -> list[DetectorReport]:
        cutoff = timezone.now() - self.age_threshold
        # A CLEAR is "forgotten" when it was issued before the cutoff
        # AND was never consumed (no MergeAudit recorded its execution).
        stale = (
            MergeClear.objects.filter(issued_at__lte=cutoff, consumed_at__isnull=True, audits__isnull=True)
            .order_by("issued_at")
            .distinct()
        )
        reports: list[DetectorReport] = []
        for clear in stale:
            pr_identity = f"{clear.slug}#{clear.pr_id}"
            reports.append(
                DetectorReport(
                    detector=self.name,
                    dedup_key=canonical_key(self.name, pr_identity),
                    state_hash=state_hash(pr_identity, clear.reviewed_sha, clear.gh_verify_result),
                    severity=self.severity,
                    max_rung=self.max_rung,
                    summary=(f"{pr_identity}: CLEAR issued at {clear.issued_at.isoformat()} but not merged"),
                    payload={
                        "pr_id": clear.pr_id,
                        "slug": clear.slug,
                        "reviewed_sha": clear.reviewed_sha,
                        "issued_at": clear.issued_at.isoformat(),
                    },
                    auto_fix=self.auto_fix,
                )
            )
        return reports

    def scan(self) -> list[ScanSignal]:
        return [report.to_signal() for report in self.detect()]
