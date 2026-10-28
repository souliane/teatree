import os
from contextlib import ExitStack
from dataclasses import replace
from datetime import datetime
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from django.test import TestCase
from django.utils import timezone

from teatree.core.cleanup.process_table import ProcessTable
from teatree.core.forge_pr_probe import GithubOpenPrHead
from teatree.core.models import DeferredQuestion, TaskAttempt, Ticket, UnshippedWorkRecord, Worktree
from teatree.core.models.checkout_custody import ABANDONED_EXIT, RELEASED_EXIT, Custody, Tenure
from teatree.core.models.worktree_occupancy import task_holder_id
from teatree.core.worktree.checkout_fence import claim_task_checkout_or_park
from teatree.core.worktree.checkout_state import checkout_state
from teatree.utils.git import run_strict
from tests.factories import PullRequestFactory, TaskFactory, TicketFactory, WorktreeFactory
from tests.teatree_core.models._shared import _init_repo_with_branch

_VENUE_CLEAR = ProcessTable(frozenset(), "/proc")
_HOST_BLIND = ProcessTable(frozenset(), "/proc", (), ("pid 7 is unreadable by this uid",))


class CheckoutSafetyTests(TestCase):
    def setUp(self) -> None:
        temporary = TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.checkout = self.root / "checkout"
        _init_repo_with_branch(self.checkout, branch="feature", commits_ahead=1)
        self.ticket = TicketFactory()
        self.task = TaskFactory(ticket=self.ticket, phase="coding")
        self.worktree = WorktreeFactory(
            ticket=self.ticket, branch="feature", extra={"worktree_path": str(self.checkout)}
        )

    def _claim(self, table: ProcessTable, venue: ProcessTable = _VENUE_CLEAR) -> TaskAttempt | None:
        with (
            patch("teatree.core.worktree.checkout_fence.read_process_table", return_value=table),
            patch("teatree.core.worktree.checkout_safety.read_process_table", return_value=venue),
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

    def _commit_file(self, name: str) -> None:
        (self.checkout / name).write_text(f"{name}\n")
        run_strict(repo=str(self.checkout), args=["add", name])
        run_strict(
            repo=str(self.checkout),
            args=["-c", "user.name=t", "-c", "user.email=t@t", "commit", "-m", name],
        )

    def _record_tenure(self, ended: str) -> None:
        prior = TaskFactory(ticket=self.ticket, phase="coding")
        custody = Custody(
            checkout_state(self.checkout, ticket=self.ticket).digest,
            (
                Tenure(
                    task_holder_id(prior),
                    "prior-session",
                    "2026-10-27T12:00:00+00:00",
                    ended,
                    "2026-10-27T12:30:00+00:00",
                ),
            ),
        )
        self.worktree.merge_extra(set_keys={"attested_state": custody.anchor, "checkout_custody": custody.as_dict()})

    def _push_with_owned_pr(self) -> ExitStack:
        remote = self.root / "remote.git"
        run_strict(repo=str(self.checkout), args=["init", "--bare", str(remote)])
        run_strict(repo=str(self.checkout), args=["remote", "add", "origin", str(remote)])
        run_strict(repo=str(self.checkout), args=["push", "origin", "feature"])
        head = run_strict(repo=str(self.checkout), args=["rev-parse", "HEAD"])
        url = "https://github.com/example/repo/pull/17"
        PullRequestFactory(ticket=self.ticket, repo="example/repo", iid="17", url=url)
        stack = ExitStack()
        probe = "teatree.core.worktree.checkout_released"
        stack.enter_context(patch(f"{probe}.git.remote_slug", return_value="example/repo"))
        stack.enter_context(
            patch(f"{probe}.git.remote_url", return_value="https://github.com/example/repo.git"),
        )
        stack.enter_context(
            patch(
                f"{probe}.forge_pr_probe.github_open_pr_head",
                return_value=GithubOpenPrHead(url, "example/repo", "feature", head),
            )
        )
        stack.enter_context(patch(f"{probe}.forge_pr_probe.github_exact_branch_head", return_value=head))
        return stack

    def _claim_pushed_head_after(self, ended: str) -> None:
        self._record_tenure(ended)
        self._commit_file("pushed.txt")
        with self._push_with_owned_pr():
            assert self._claim(_HOST_BLIND) is None
        self.worktree.refresh_from_db()
        assert self.worktree.get_extra()["auto_attestation"]["process_evidence"] == "venue"

    def test_pushed_head_claims_after_a_released_tenure_when_the_host_table_is_blind(self) -> None:
        self._claim_pushed_head_after(RELEASED_EXIT)

    def test_pushed_head_claims_after_an_abandoned_tenure_when_the_host_table_is_blind(self) -> None:
        self._claim_pushed_head_after(ABANDONED_EXIT)

    def test_foreign_edit_after_a_released_tenure_still_parks_when_the_host_table_is_blind(self) -> None:
        self._record_tenure(RELEASED_EXIT)
        self._commit_file("pushed.txt")
        (self.checkout / "foreign.txt").write_text("unowned")
        with self._push_with_owned_pr():
            attempt = self._claim(_HOST_BLIND)
        assert attempt is not None
        assert attempt.error.startswith("checkout_parked:")

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

    def test_custody_recovery_preserves_unpushed_commits_before_claim(self) -> None:
        Worktree.objects.filter(pk=self.worktree.pk).update(branch="feature")
        self._record_prior_writer()

        assert self._claim(ProcessTable(frozenset(), "/proc")) is None
        assert UnshippedWorkRecord.objects.filter(checkout_path=str(self.checkout)).exists()

    def test_unpushed_commits_on_an_unowned_branch_park_after_preservation(self) -> None:
        self._record_prior_writer()
        run_strict(repo=str(self.checkout), args=["checkout", "-b", "other"])

        attempt = self._claim(ProcessTable(frozenset(), "/proc"))

        assert attempt is not None
        assert "owned pull request" in attempt.error
        assert UnshippedWorkRecord.objects.filter(checkout_path=str(self.checkout)).exists()

    def test_unpushed_commits_without_writer_history_park(self) -> None:
        attempt = self._claim(ProcessTable(frozenset(), "/proc"))
        assert attempt is not None
        assert "owned pull request" in attempt.error

    def test_unwritable_bundle_parks_before_claim(self) -> None:
        self._record_prior_writer()
        with patch("teatree.core.worktree.checkout_safety.capture_unshipped_work", return_value=None):
            attempt = self._claim(ProcessTable(frozenset(), "/proc"))
        assert attempt is not None
        assert "could not be preserved" in attempt.error

    def test_unreadable_bundle_readback_parks_before_claim(self) -> None:
        self._record_prior_writer()
        with patch("teatree.core.worktree.checkout_safety.bundle_path") as artifact:
            artifact.return_value.is_file.return_value = True
            artifact.return_value.read_text.side_effect = OSError("unreadable")
            attempt = self._claim(ProcessTable(frozenset(), "/proc"))
        assert attempt is not None
        assert "could not be preserved" in attempt.error

    def test_unusable_host_table_uses_venue_proof_and_a_lease(self) -> None:
        self._record_prior_writer()
        assert self._claim(ProcessTable(frozenset(), "", ("host table unavailable",))) is None
        self.worktree.refresh_from_db()
        assert self.worktree.get_extra()["auto_attestation"]["process_evidence"] == "venue"

    def test_unreadable_venue_processes_park_and_the_reason_names_them(self) -> None:
        self._record_prior_writer()
        host = ProcessTable(frozenset(), "", ("host table unavailable",))
        venue = ProcessTable(frozenset(), "/proc", (), ("pid 7 is unreadable by this uid",))
        attempt = self._claim(host, venue)
        assert attempt is not None
        assert attempt.error.endswith("process table unavailable: pid 7 is unreadable by this uid")

    def test_venue_proof_parks_when_a_process_works_inside_the_checkout(self) -> None:
        self._record_prior_writer()
        attempt = self._claim(_HOST_BLIND, ProcessTable(frozenset({self.checkout}), "/proc"))
        assert attempt is not None
        assert "live process in this venue" in attempt.error

    def test_venue_proof_needs_the_claimants_exclusive_lease(self) -> None:
        self._record_prior_writer()
        with patch("teatree.core.worktree.checkout_safety.occupancy_holder", return_value=None):
            attempt = self._claim(_HOST_BLIND)
        assert attempt is not None
        assert "no exclusive claimant lease" in attempt.error

    def test_foreign_edit_parks_quietly_then_retries(self) -> None:
        Worktree.objects.filter(pk=self.worktree.pk).update(branch="feature")
        self._record_prior_writer()
        foreign = self.checkout / "foreign.txt"
        foreign.write_text("unowned")
        attempt = self._claim(ProcessTable(frozenset(), "/proc"))
        assert attempt is not None
        assert attempt.error.startswith("checkout_parked:")
        foreign.unlink()
        assert self._claim(ProcessTable(frozenset(), "/proc")) is None

    def _prior_writer_ended_at(self, when: datetime) -> None:
        row = Worktree.objects.get(pk=self.worktree.pk)
        custody = Custody.from_extra(row.get_extra())
        assert custody is not None
        ended = replace(custody.tenures[-1], ended_at=when.isoformat())
        row.merge_extra(set_keys={"checkout_custody": Custody(custody.anchor, (ended,)).as_dict()})

    def _assert_parked(self) -> None:
        attempt = self._claim(ProcessTable(frozenset(), "/proc"))
        assert attempt is not None
        assert attempt.error.startswith("checkout_parked:")

    def _assert_reattested(self) -> None:
        assert self._claim(ProcessTable(frozenset(), "/proc")) is None
        extra = Worktree.objects.get(pk=self.worktree.pk).get_extra()
        assert extra["attested_state"] == checkout_state(self.checkout, ticket=self.ticket).digest

    def test_new_file_the_interrupted_writer_left_before_it_ended_is_reattested(self) -> None:
        self._record_prior_writer()
        (self.checkout / "unfinished.txt").write_text("the writer's own work")
        self._prior_writer_ended_at(timezone.now())

        self._assert_reattested()

        assert (self.checkout / "unfinished.txt").read_text() == "the writer's own work"

    def test_tracked_file_the_interrupted_writer_deleted_is_reattested(self) -> None:
        self._record_prior_writer()
        (self.checkout / "README.md").unlink()
        self._prior_writer_ended_at(timezone.now())

        self._assert_reattested()

    def test_file_the_interrupted_writer_renamed_is_reattested(self) -> None:
        self._record_prior_writer()
        run_strict(repo=str(self.checkout), args=["mv", "f0.txt", "moved.txt"])
        self._prior_writer_ended_at(timezone.now())

        self._assert_reattested()

    def test_tracked_file_deleted_after_the_interrupted_writer_ended_parks(self) -> None:
        self._record_prior_writer()
        (self.checkout / "README.md").unlink()

        self._assert_parked()

    def test_foreign_file_with_a_backdated_modification_time_still_parks(self) -> None:
        self._record_prior_writer()
        foreign = self.checkout / "foreign.txt"
        foreign.write_text("unowned")
        os.utime(foreign, (0, 0))

        self._assert_parked()

    def test_live_process_parks_without_a_question(self) -> None:
        attempt = self._claim(ProcessTable(frozenset({self.checkout}), "/proc"))
        assert attempt is not None
        assert attempt.error.startswith("checkout_parked:")

    def test_merge_in_progress_parks_before_claim(self) -> None:
        self._record_prior_writer()
        head = run_strict(repo=str(self.checkout), args=["rev-parse", "HEAD"])
        (self.checkout / ".git" / "MERGE_HEAD").write_text(f"{head}\n")

        attempt = self._claim(_VENUE_CLEAR)

        assert attempt is not None
        assert "a merge in progress" in attempt.error

    def test_rebase_in_progress_parks_before_claim(self) -> None:
        self._record_prior_writer()
        (self.checkout / ".git" / "rebase-apply").mkdir()

        attempt = self._claim(_VENUE_CLEAR)

        assert attempt is not None
        assert "a rebase in progress" in attempt.error

    def test_detached_head_parks_before_claim(self) -> None:
        self._record_prior_writer()
        run_strict(repo=str(self.checkout), args=["checkout", "--detach", "HEAD"])

        attempt = self._claim(_VENUE_CLEAR)

        assert attempt is not None
        assert "detached HEAD" in attempt.error

    def test_index_lock_parks_before_claim(self) -> None:
        self._record_prior_writer()
        (self.checkout / ".git" / "index.lock").touch()

        attempt = self._claim(_VENUE_CLEAR)

        assert attempt is not None
        assert "index lock" in attempt.error

    def _hand_off_from(self, *earlier: tuple[Ticket, str]) -> None:
        tenures = tuple(
            Tenure(
                task_holder_id(TaskFactory(ticket=ticket, phase=phase)),
                "prior",
                "2026-10-27T12:00:00+00:00",
                RELEASED_EXIT,
                "2026-10-27T12:30:00+00:00",
            )
            for ticket, phase in earlier
        )
        custody = Custody(checkout_state(self.checkout, ticket=self.ticket).digest, tenures)
        self.worktree.merge_extra(set_keys={"attested_state": custody.anchor, "checkout_custody": custody.as_dict()})
        self._commit_file("handed-off.txt")
        self.task = TaskFactory(ticket=self.ticket, phase="testing")

    def test_hand_off_between_phases_of_one_ticket_bundles_unpushed_commits_and_claims(self) -> None:
        self._hand_off_from((self.ticket, "coding"), (self.ticket, "reviewing"))
        assert self._claim(ProcessTable(frozenset(), "/proc")) is None
        record = UnshippedWorkRecord.objects.get(checkout_path=str(self.checkout))
        assert len(record.unpushed_commits) == 3
        assert Path(f"{record.artifact_prefix}.commits.patch").stat().st_size > 0

    def test_hand_off_after_a_tenure_of_another_ticket_still_parks(self) -> None:
        self._hand_off_from((TicketFactory(), "coding"), (self.ticket, "coding"))
        attempt = self._claim(ProcessTable(frozenset(), "/proc"))
        assert attempt is not None
        assert "owned pull request" in attempt.error

    def test_hand_off_whose_latest_tenure_is_another_tickets_still_parks(self) -> None:
        self._hand_off_from((self.ticket, "coding"), (TicketFactory(), "coding"))
        attempt = self._claim(ProcessTable(frozenset(), "/proc"))
        assert attempt is not None
        assert "owned pull request" in attempt.error

    def test_hand_off_after_the_ticket_opened_a_pull_request_still_needs_the_owned_pr(self) -> None:
        PullRequestFactory(ticket=self.ticket)
        self._hand_off_from((self.ticket, "coding"), (self.ticket, "reviewing"))
        attempt = self._claim(ProcessTable(frozenset(), "/proc"))
        assert attempt is not None
        assert "owned pull request" in attempt.error

    def test_hand_off_parks_when_the_bundle_does_not_match(self) -> None:
        self._hand_off_from((self.ticket, "coding"), (self.ticket, "reviewing"))
        with patch("teatree.core.worktree.checkout_safety.bundle_path") as artifact:
            artifact.return_value.is_file.return_value = True
            artifact.return_value.read_text.return_value = "not the patch"
            attempt = self._claim(ProcessTable(frozenset(), "/proc"))
        assert attempt is not None
        assert "could not be preserved" in attempt.error

    def test_hand_off_parks_when_the_bundle_cannot_be_written(self) -> None:
        self._hand_off_from((self.ticket, "coding"), (self.ticket, "reviewing"))
        with patch("teatree.core.worktree.checkout_safety.capture_unshipped_work", return_value=None):
            attempt = self._claim(ProcessTable(frozenset(), "/proc"))
        assert attempt is not None
        assert "could not be preserved" in attempt.error

    def test_pushed_branch_without_an_owned_pr_still_parks_after_a_released_hand_off(self) -> None:
        self._hand_off_from((self.ticket, "coding"), (self.ticket, "reviewing"))
        remote = self.root / "remote.git"
        run_strict(repo=str(self.checkout), args=["init", "--bare", str(remote)])
        run_strict(repo=str(self.checkout), args=["remote", "add", "origin", str(remote)])
        run_strict(repo=str(self.checkout), args=["push", "origin", "feature"])
        attempt = self._claim(ProcessTable(frozenset(), "/proc"))
        assert attempt is not None
        assert "owned pull request" in attempt.error
