"""Health signals for overdue pull request obligations."""

from teatree.core.factory.health_signal import HealthSignal, SignalCollection
from teatree.core.models.known_issue import KnownIssue
from teatree.core.models.pending_pull_request import PendingPullRequest


def pending_pr_overdue_signals() -> SignalCollection:
    return SignalCollection(
        tuple(
            HealthSignal(
                fingerprint=f"pending-pr:{row.pk}",
                severity=KnownIssue.Severity.WARNING,
                kind="pending_pr_overdue",
                summary=f"pull request obligation for {row.branch} remains overdue: {row.last_error}",
            )
            for row in PendingPullRequest.objects.overdue()
        )
    )
