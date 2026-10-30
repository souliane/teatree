from pathlib import Path

from django.test import TestCase

from teatree.core.factory.operational_health import collect_signals
from teatree.core.models import PendingPullRequest
from teatree.core.models.pending_pull_request import MAX_DRAIN_ATTEMPTS


class PendingPrHealthTests(TestCase):
    def test_one_overdue_obligation_has_one_health_signal(self) -> None:
        row = PendingPullRequest.objects.owe(repo_path=str(Path.cwd()), branch="feat/owed", reason="pending")
        PendingPullRequest.objects.filter(pk=row.pk).update(drain_attempts=MAX_DRAIN_ATTEMPTS)

        signals = [signal for signal in collect_signals().signals if signal.kind == "pending_pr_overdue"]

        assert len(signals) == 1
        assert signals[0].fingerprint == f"pending-pr:{row.pk}"
