"""``ForgottenMergeDetector`` — a green/mergeable PR the merge loop never merged.

Fires when a standing ``MergeClear`` older than 30 minutes authorises a PR the
forge still reports OPEN — the keystone CLEARED the diff and the loop has not
consumed the authorisation.

Reads the canonical backlog
(:func:`~teatree.core.factory.merge_backlog.unconsumed_actionable_clear_rows`) and
the shared classifier
(:func:`~teatree.core.merge.clear_liveness.classify`) rather than its own query.
Asking the same question from a second, weaker query — "no ``MergeAudit`` row" with
no supersede exclusion and no liveness — is what made all 8 of its live firings
false: every one had already merged, or named a PR that does not exist.

Severity is ``error``: a stalled keystone merge means a green-CI PR is sitting
unmerged, which is the exact failure class § 17.4.4 attests against.  The ladder
ceiling is ``slack`` per the issue plan (statusline → slack); ``auto_fix`` is
``False`` (the loop merges via the keystone transition, never raw ``gh pr merge`` —
re-issuing the merge is a loop-side action gated by independent review).

The forge reader is injected and defaults to
:func:`~teatree.core.merge.clear_liveness.unverified_reader`, so a detector nobody
wired reports nothing instead of paging on unread rows.
"""

import datetime as dt
from dataclasses import dataclass, field
from typing import ClassVar

from django.utils import timezone

from teatree.loop.scanners.base import ScanSignal
from teatree.loop.scanners.clear_stall_lookup import PROBE_CAP, PrStateReader, stalled_clears, unverified_reader
from teatree.loop.self_improve.dedup import canonical_key, state_hash
from teatree.loop.self_improve.detectors.base import ActionRung, DetectorReport

DEFAULT_AGE_THRESHOLD = dt.timedelta(minutes=30)


@dataclass(slots=True)
class ForgottenMergeDetector:
    """A CLEAR older than 30 min whose PR the forge still reports OPEN."""

    name: ClassVar[str] = "forgotten_merge"
    tier: ClassVar[str] = "cheap"
    severity: ClassVar[str] = "error"
    max_rung: ClassVar[str] = ActionRung.SLACK
    auto_fix: ClassVar[bool] = False

    age_threshold: dt.timedelta = DEFAULT_AGE_THRESHOLD
    read_state: PrStateReader = field(default=unverified_reader)
    probe_cap: int = PROBE_CAP

    def detect(self) -> list[DetectorReport]:
        cutoff = timezone.now() - self.age_threshold
        stalled = stalled_clears(issued_before=cutoff, read_state=self.read_state, cap=self.probe_cap)
        reports: list[DetectorReport] = []
        for clear in stalled:
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
