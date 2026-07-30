"""``check_pending_pull_requests`` — a PR owed since push must fail loud, not retry in silence."""

import django.test
import pytest

from teatree.cli.doctor.checks_pending_pr import check_pending_pull_requests
from teatree.core.models import PendingPullRequest
from teatree.core.models.pending_pull_request import MAX_DRAIN_ATTEMPTS
from tests.factories import serialized_pr_spec


class PendingPullRequestDoctorCheckTestCase(django.test.TestCase):
    @pytest.fixture(autouse=True)
    def _inject_fixtures(self, capsys: pytest.CaptureFixture[str]) -> None:
        self._capsys = capsys

    @staticmethod
    def _owed(*, attempts: int) -> PendingPullRequest:
        row = PendingPullRequest.objects.owe(
            repo_path="/w/clone",
            branch="feat/orphan",
            reason="branch not on remote yet",
            spec=serialized_pr_spec("feat: the feature"),
        )
        PendingPullRequest.objects.filter(pk=row.pk).update(drain_attempts=attempts)
        row.refresh_from_db()
        return row

    def test_no_obligations_pass(self) -> None:
        assert check_pending_pull_requests() is True

    def test_fresh_obligation_passes_while_the_drain_still_has_ticks(self) -> None:
        self._owed(attempts=MAX_DRAIN_ATTEMPTS - 1)
        assert check_pending_pull_requests() is True

    def test_undrainable_obligation_fails_loud_naming_branch_and_intended_pr(self) -> None:
        self._owed(attempts=MAX_DRAIN_ATTEMPTS)

        assert check_pending_pull_requests() is False

        out = self._capsys.readouterr().out
        assert "FAIL" in out
        assert "feat/orphan" in out
        assert "feat: the feature" in out
        assert "pr ensure-pr" in out
