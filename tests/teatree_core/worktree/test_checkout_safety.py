from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from django.test import TestCase

from teatree.core.cleanup.process_table import ProcessTable
from teatree.core.forge_pr_probe import GithubOpenPrHead
from teatree.core.models import DeferredQuestion, TaskAttempt, UnshippedWorkRecord
from teatree.core.models.checkout_custody import ABANDONED_EXIT, Custody, Tenure
from teatree.core.models.worktree_occupancy import task_holder_id
from teatree.core.worktree.checkout_fence import claim_task_checkout_or_park
from teatree.core.worktree.checkout_state import checkout_state
from teatree.utils.git import run_strict
from tests.factories import PullRequestFactory, TaskFactory, TicketFactory, WorktreeFactory
from tests.teatree_core.models._shared import _init_repo_with_branch


class CheckoutSafetyTests(TestCase):
    def setUp(self) -> None:
        temporary = TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.checkout = self.root / "checkout"
        _init_repo_with_branch(self.checkout, branch="feature", commits_ahead=1)
        self.ticket = TicketFactory()
        self.task = TaskFactory(ticket=self.ticket, phase="coding")
        self.worktree = WorktreeFactory(ticket=self.ticket, extra={"worktree_path": str(self.checkout)})

    def _claim(self, table: ProcessTable) -> TaskAttempt | None:
        with (
            patch("teatree.core.worktree.checkout_fence.read_process_table", return_value=table),
            patch("teatree.core.worktree.checkout_fence.notify_user", create=True) as notify,
        ):
            attempt = claim_task_checkout_or_park(self.task)
        notify.assert_not_called()
        assert not DeferredQuestion.objects.filter(task_session=self.task.session).exists()
        return attempt

    def _record_prior_writer(self) -> None:
        prior = TaskFactory(ticket=self.ticket, phase="coding")
        custody = Custody(
            checkout_state(self.checkout, ticket=self.ticket).digest,
            (
                Tenure(
                    task_holder_id(prior),
                    "prior-session",
                    "2026-10-27T12:00:00+00:00",
                    ABANDONED_EXIT,
                    "2026-10-27T12:30:00+00:00",
                ),
            ),
        )
        self.worktree.merge_extra(set_keys={"attested_state": custody.anchor, "checkout_custody": custody.as_dict()})
        (self.checkout / "owned.txt").write_text("owned\n")
        run_strict(repo=str(self.checkout), args=["add", "owned.txt"])
        run_strict(
            repo=str(self.checkout),
            args=["-c", "user.name=t", "-c", "user.email=t@t", "commit", "-m", "owned"],
        )

    def test_pushed_clean_branch_claims_and_records_proof(self) -> None:
        remote = self.root / "remote.git"
        run_strict(repo=str(self.checkout), args=["init", "--bare", str(remote)])
        run_strict(repo=str(self.checkout), args=["remote", "add", "origin", str(remote)])
        run_strict(repo=str(self.checkout), args=["push", "origin", "feature"])
        head = run_strict(repo=str(self.checkout), args=["rev-parse", "HEAD"])
        url = "https://github.com/example/repo/pull/17"
        PullRequestFactory(ticket=self.ticket, repo="example/repo", iid="17", url=url)

        with (
            patch("teatree.core.worktree.checkout_released.git.remote_slug", return_value="example/repo"),
            patch(
                "teatree.core.worktree.checkout_released.git.remote_url",
                return_value="https://github.com/example/repo.git",
            ),
            patch(
                "teatree.core.worktree.checkout_released.forge_pr_probe.github_open_pr_head",
                return_value=GithubOpenPrHead(url, "example/repo", "feature", head),
            ),
            patch(
                "teatree.core.worktree.checkout_released.forge_pr_probe.github_exact_branch_head",
                return_value=head,
            ),
        ):
            assert self._claim(ProcessTable(frozenset(), "/proc")) is None
        self.worktree.refresh_from_db()
        assert self.worktree.get_extra()["auto_attestation"]["process_evidence"] == "host"

    def test_pushed_branch_without_an_owned_pr_parks(self) -> None:
        remote = self.root / "remote.git"
        run_strict(repo=str(self.checkout), args=["init", "--bare", str(remote)])
        run_strict(repo=str(self.checkout), args=["remote", "add", "origin", str(remote)])
        run_strict(repo=str(self.checkout), args=["push", "origin", "feature"])

        attempt = self._claim(ProcessTable(frozenset(), "/proc"))
        assert attempt is not None
        assert "owned pull request" in attempt.error

    def test_unpushed_commits_are_bundled_before_claim(self) -> None:
        self._record_prior_writer()
        assert self._claim(ProcessTable(frozenset(), "/proc")) is None
        record = UnshippedWorkRecord.objects.get(checkout_path=str(self.checkout))
        assert len(record.unpushed_commits) == 3
        assert Path(f"{record.artifact_prefix}.commits.patch").stat().st_size > 0

    def test_unpushed_commits_without_writer_history_park(self) -> None:
        attempt = self._claim(ProcessTable(frozenset(), "/proc"))
        assert attempt is not None
        assert "recorded ticket writer" in attempt.error

    def test_unwritable_bundle_parks_before_claim(self) -> None:
        self._record_prior_writer()
        with patch("teatree.core.worktree.checkout_safety.capture_unshipped_work", return_value=None):
            attempt = self._claim(ProcessTable(frozenset(), "/proc"))
        assert attempt is not None
        assert "could not be preserved" in attempt.error

    def test_unusable_host_table_uses_venue_proof_and_a_lease(self) -> None:
        self._record_prior_writer()
        assert self._claim(ProcessTable(frozenset(), "", ("host table unavailable",))) is None
        self.worktree.refresh_from_db()
        assert self.worktree.get_extra()["auto_attestation"]["process_evidence"] == "venue"

    def test_foreign_edit_parks_quietly_then_retries(self) -> None:
        self._record_prior_writer()
        foreign = self.checkout / "foreign.txt"
        foreign.write_text("unowned")
        attempt = self._claim(ProcessTable(frozenset(), "/proc"))
        assert attempt is not None
        assert attempt.error.startswith("checkout_parked:")
        foreign.unlink()
        assert self._claim(ProcessTable(frozenset(), "/proc")) is None

    def test_live_process_parks_without_a_question(self) -> None:
        attempt = self._claim(ProcessTable(frozenset({self.checkout}), "/proc"))
        assert attempt is not None
        assert attempt.error.startswith("checkout_parked:")
