"""``PendingPrDrainScanner`` — the drain the pre-push deferral never had.

Real git under ``tmp_path`` throughout: the obligation is created by the real
``ensure-pr`` deferral, and the drain's verdict is decided by the real branch
state on a real remote, not by a patched classifier.
"""

import shutil
from datetime import timedelta
from pathlib import Path
from unittest.mock import patch

import pytest
from django.core.management import call_command
from django.db import connection
from django.test import TestCase
from django.test.utils import CaptureQueriesContext
from django.utils import timezone

from teatree.cli.doctor.checks_pending_pr import check_pending_pull_requests
from teatree.core.models import PendingPullRequest
from teatree.core.models.deferred_question import DeferredQuestion
from teatree.core.models.pending_pull_request import MAX_DRAIN_ATTEMPTS
from teatree.core.owner_dm_sweep import run_sweep
from teatree.loop.scanners import PendingPrDrainScanner
from tests.teatree_core.cleanup._shared import _run_git


def _first_push_repo(tmp_path: Path) -> tuple[Path, Path, str]:
    """``(origin, clone, branch)`` where ``branch`` carries work git has never pushed."""
    origin = tmp_path / "origin.git"
    origin.mkdir()
    _run_git("init", "--bare", "--initial-branch=main", cwd=origin)

    repo = tmp_path / "clone"
    repo.mkdir()
    _run_git("init", "--initial-branch=main", cwd=repo)
    _run_git("config", "user.email", "t@example.com", cwd=repo)
    _run_git("config", "user.name", "T", cwd=repo)
    (repo / "README.md").write_text("base\n")
    _run_git("add", "README.md", cwd=repo)
    _run_git("commit", "-m", "chore: base", cwd=repo)
    _run_git("remote", "add", "origin", str(origin), cwd=repo)
    _run_git("push", "-u", "origin", "main", cwd=repo)
    _run_git("remote", "set-head", "origin", "main", cwd=repo)

    _run_git("checkout", "-b", "feat/orphan", cwd=repo)
    (repo / "feature.py").write_text("value = 1\n")
    _run_git("add", "feature.py", cwd=repo)
    _run_git("commit", "-m", "feat: add the feature", cwd=repo)
    return origin, repo, "feat/orphan"


class PendingPrDrainScannerTestCase(TestCase):
    def test_far_overdue_obligation_gets_one_owner_card_with_plain_cause(self) -> None:
        _origin, repo, branch = _first_push_repo(self._tmp_path)
        call_command("pr", "ensure-pr", repo=str(repo), branch=branch)
        PendingPullRequest.objects.filter(branch=branch).update(drain_attempts=4386)
        scanner = PendingPrDrainScanner()
        scanner.scan()
        scanner.scan()
        row = DeferredQuestion.objects.get()
        assert row.evidence["decision"] == "irreversible"
        assert "remote" in row.evidence["why"]
        assert str(repo) not in str(row.evidence)

    def test_two_stuck_obligations_stay_two_cards_through_the_owner_dm_sweep(self) -> None:
        self._overdue_obligation("first")
        self._overdue_obligation("second")
        PendingPrDrainScanner().scan()
        assert run_sweep().resolved == 0
        assert DeferredQuestion.owner_pending().count() == 2

    def test_card_names_the_branch_so_the_owner_can_tell_obligations_apart(self) -> None:
        _origin, repo, branch = _first_push_repo(self._tmp_path)
        call_command("pr", "ensure-pr", repo=str(repo), branch=branch)
        PendingPullRequest.objects.update(drain_attempts=MAX_DRAIN_ATTEMPTS)
        PendingPrDrainScanner().scan()
        assert branch in DeferredQuestion.objects.get().evidence["quoted"]

    def test_owner_stop_reminding_discharges_the_obligation(self) -> None:
        _origin, repo, branch = _first_push_repo(self._tmp_path)
        call_command("pr", "ensure-pr", repo=str(repo), branch=branch)
        PendingPullRequest.objects.filter(branch=branch).update(drain_attempts=4386)
        scanner = PendingPrDrainScanner()
        scanner.scan()
        DeferredQuestion.objects.update(
            answered_at=timezone.now(), answer_text="Stop reminding", resolved_via=DeferredQuestion.ResolvedVia.SLACK
        )
        scanner.scan()
        assert not PendingPullRequest.objects.filter(branch=branch).exists()

    def test_owner_stop_reminding_keeps_a_due_obligation_from_being_drained(self) -> None:
        _origin, repo, branch = _first_push_repo(self._tmp_path)
        call_command("pr", "ensure-pr", repo=str(repo), branch=branch)
        PendingPullRequest.objects.filter(branch=branch).update(drain_attempts=4386)
        scanner = PendingPrDrainScanner()
        scanner.scan()
        PendingPullRequest.objects.update(last_attempt_at=None)
        DeferredQuestion.objects.update(
            answered_at=timezone.now(), answer_text="Stop reminding", resolved_via=DeferredQuestion.ResolvedVia.SLACK
        )
        with patch("django.core.management.call_command") as ensure_pr:
            scanner.scan()
        ensure_pr.assert_not_called()
        assert not PendingPullRequest.objects.filter(branch=branch).exists()

    def test_local_answer_decides_nothing_and_the_card_is_asked_again(self) -> None:
        _origin, repo, branch = _first_push_repo(self._tmp_path)
        call_command("pr", "ensure-pr", repo=str(repo), branch=branch)
        PendingPullRequest.objects.filter(branch=branch).update(drain_attempts=4386)
        scanner = PendingPrDrainScanner()
        scanner.scan()
        DeferredQuestion.objects.update(
            answered_at=timezone.now(), answer_text="Stop reminding", resolved_via=DeferredQuestion.ResolvedVia.LOCAL
        )
        scanner.scan()
        assert PendingPullRequest.objects.filter(branch=branch).exists()
        assert DeferredQuestion.owner_pending().count() == 1

    def test_steady_state_question_queries_do_not_grow_with_overdue_rows(self) -> None:
        scanner = PendingPrDrainScanner()
        self._overdue_obligation("first")
        scanner.scan()
        one_row = self._question_queries(scanner)
        self._overdue_obligation("second")
        scanner.scan()
        assert self._question_queries(scanner) == one_row

    def test_card_is_settled_when_another_path_discharges_the_obligation(self) -> None:
        _origin, repo, branch = _first_push_repo(self._tmp_path)
        call_command("pr", "ensure-pr", repo=str(repo), branch=branch)
        PendingPullRequest.objects.filter(branch=branch).update(drain_attempts=MAX_DRAIN_ATTEMPTS)
        scanner = PendingPrDrainScanner()
        scanner.scan()
        assert DeferredQuestion.owner_pending().count() == 1
        PendingPullRequest.objects.discharge(repo_path=str(repo), branch=branch)
        scanner.scan()
        assert not DeferredQuestion.owner_pending().exists()

    def test_card_is_settled_when_the_checkout_is_gone(self) -> None:
        _origin, repo, branch = _first_push_repo(self._tmp_path)
        call_command("pr", "ensure-pr", repo=str(repo), branch=branch)
        PendingPullRequest.objects.filter(branch=branch).update(drain_attempts=MAX_DRAIN_ATTEMPTS)
        scanner = PendingPrDrainScanner()
        scanner.scan()
        shutil.rmtree(repo)
        scanner.scan()
        assert not DeferredQuestion.owner_pending().exists()

    def test_keep_trying_leaves_obligation_and_does_not_ask_again(self) -> None:
        _origin, repo, branch = _first_push_repo(self._tmp_path)
        call_command("pr", "ensure-pr", repo=str(repo), branch=branch)
        PendingPullRequest.objects.filter(branch=branch).update(drain_attempts=MAX_DRAIN_ATTEMPTS)
        scanner = PendingPrDrainScanner()
        scanner.scan()
        DeferredQuestion.objects.update(
            answered_at=timezone.now(), answer_text="Keep trying", resolved_via=DeferredQuestion.ResolvedVia.SLACK
        )
        scanner.scan()
        assert PendingPullRequest.objects.filter(branch=branch).exists()
        assert DeferredQuestion.objects.count() == 1

    @pytest.fixture(autouse=True)
    def _inject_fixtures(self, tmp_path: Path) -> None:
        self._tmp_path = tmp_path

    def _overdue_obligation(self, name: str) -> None:
        (self._tmp_path / name).mkdir()
        _origin, repo, branch = _first_push_repo(self._tmp_path / name)
        call_command("pr", "ensure-pr", repo=str(repo), branch=branch)
        PendingPullRequest.objects.filter(branch=branch, repo_path=str(repo)).update(drain_attempts=MAX_DRAIN_ATTEMPTS)

    @staticmethod
    def _question_queries(scanner: PendingPrDrainScanner) -> int:
        with CaptureQueriesContext(connection) as queries:
            scanner.scan()
        return sum("teatree_deferred_question" in query["sql"] for query in queries)

    def test_no_obligations_emits_nothing(self) -> None:
        assert PendingPrDrainScanner().scan() == []

    def test_backed_off_old_rows_do_not_hide_a_new_due_obligation(self) -> None:
        repo = self._tmp_path / "checkout"
        repo.mkdir()
        for index in range(50):
            row = PendingPullRequest.objects.owe(repo_path=str(repo), branch=f"feat/paused-{index}", reason="waiting")
            PendingPullRequest.objects.filter(pk=row.pk).update(
                drain_attempts=MAX_DRAIN_ATTEMPTS, last_attempt_at=timezone.now()
            )
        due = PendingPullRequest.objects.owe(repo_path=str(repo), branch="feat/due", reason="waiting")
        with patch("django.core.management.call_command", return_value={"created": True}) as ensure:
            signals = PendingPrDrainScanner().scan()
        assert [signal.kind for signal in signals] == ["pending_pr.drained"]
        ensure.assert_called_once_with("pr", "ensure-pr", repo=str(repo), branch=due.branch)
        assert not PendingPullRequest.objects.filter(pk=due.pk).exists()
        assert PendingPullRequest.objects.count() == 50

    def test_overdue_obligation_backs_off_and_asks_the_owner_once(self) -> None:
        repo = self._tmp_path / "checkout"
        repo.mkdir()
        row = PendingPullRequest.objects.owe(repo_path=str(repo), branch="feat/stuck", reason="push pending")
        start = timezone.now()
        with (
            patch("django.core.management.call_command", return_value={"owed": True}) as ensure,
            patch("django.utils.timezone.now") as clock,
        ):
            for tick in range(2333):
                clock.return_value = start + timedelta(minutes=5 * tick)
                PendingPrDrainScanner().scan()
        row.refresh_from_db()
        assert ensure.call_count <= 30
        assert row.drain_attempts == ensure.call_count
        assert DeferredQuestion.objects.filter(dedupe_marker=f"pending-pr-stuck:{row.pk}").count() == 1

    def test_branch_still_unpushed_keeps_the_obligation_and_ages_it(self) -> None:
        _origin, repo, branch = _first_push_repo(self._tmp_path)
        call_command("pr", "ensure-pr", repo=str(repo), branch=branch)

        assert PendingPrDrainScanner().scan() == []

        owed = PendingPullRequest.objects.get(branch=branch)
        assert owed.drain_attempts == 1
        assert "not on remote yet" in owed.last_error

    def test_branch_landed_on_the_default_branch_discharges_the_obligation(self) -> None:
        """A branch merged while the obligation waited needs no PR — and gets none."""
        _origin, repo, branch = _first_push_repo(self._tmp_path)
        call_command("pr", "ensure-pr", repo=str(repo), branch=branch)
        _run_git("checkout", "main", cwd=repo)
        _run_git("merge", "--ff-only", branch, cwd=repo)
        _run_git("push", "origin", "main", cwd=repo)

        signals = PendingPrDrainScanner().scan()

        assert [signal.kind for signal in signals] == ["pending_pr.drained"]
        assert not PendingPullRequest.objects.filter(branch=branch).exists()

    def test_an_obligation_stored_under_a_symlinked_path_is_still_retired(self) -> None:
        _origin, repo, branch = _first_push_repo(self._tmp_path)
        link = self._tmp_path / "clone-link"
        link.symlink_to(repo)
        PendingPullRequest.objects.owe(repo_path=str(link), branch=branch, reason="branch not on remote yet")
        _run_git("checkout", "main", cwd=repo)
        _run_git("merge", "--ff-only", branch, cwd=repo)
        _run_git("push", "origin", "main", cwd=repo)

        PendingPrDrainScanner().scan()

        assert not PendingPullRequest.objects.filter(branch=branch).exists()

    def test_work_that_reached_the_base_under_a_different_path_discharges(self) -> None:
        """#3977: the same bytes landed while a refactor moved them — the branch owes nothing.

        Without the content premise re-test the obligation renews on every tick
        forever, and its remedy opens a PR that reverts the base.
        """
        _origin, repo, branch = _first_push_repo(self._tmp_path)
        call_command("pr", "ensure-pr", repo=str(repo), branch=branch)
        _run_git("checkout", "main", cwd=repo)
        (repo / "core").mkdir()
        (repo / "core" / "feature.py").write_text("value = 1\n")
        _run_git("add", "-A", cwd=repo)
        _run_git("commit", "-m", "refactor(core): the same fix, under the split module", cwd=repo)
        _run_git("push", "origin", "main", cwd=repo)

        signals = PendingPrDrainScanner().scan()

        assert [signal.kind for signal in signals] == ["pending_pr.drained"]
        assert not PendingPullRequest.objects.filter(branch=branch).exists()

    def test_content_that_squash_merged_then_came_back_as_a_merge_discharges(self) -> None:
        """#4429: the branch is ahead by SHA and delivers nothing — a PR would be empty.

        Renewing here is what re-opened a PR on already-merged content twice in one
        evening, each costing a full cold review it could not repay.

        The branch needs a SECOND commit whose subject the squash commit cannot
        absorb: a single-commit branch whose only subject the squash-commit's
        `(#N)`-suffix match reuses makes `genuinely_ahead` empty, so the branch
        reads SYNCED at the pre-existing `ahead == 0` arm — never reaching the
        layer this test means to guard (review finding on #4550).
        """
        _origin, repo, branch = _first_push_repo(self._tmp_path)
        call_command("pr", "ensure-pr", repo=str(repo), branch=branch)
        (repo / "test_feature.py").write_text("def test_feature() -> None:\n    assert value == 1\n")
        _run_git("add", "test_feature.py", cwd=repo)
        _run_git("commit", "-m", "test: cover the feature", cwd=repo)
        _run_git("checkout", "main", cwd=repo)
        _run_git("merge", "--squash", branch, cwd=repo)
        _run_git("commit", "-m", "feat: add the feature (#4422)", cwd=repo)
        _run_git("push", "origin", "main", cwd=repo)
        _run_git("checkout", branch, cwd=repo)
        _run_git("merge", "--no-edit", "origin/main", cwd=repo)
        _run_git("push", "origin", branch, cwd=repo)

        signals = PendingPrDrainScanner().scan()

        assert [signal.kind for signal in signals] == ["pending_pr.drained"]
        assert not PendingPullRequest.objects.filter(branch=branch).exists()

    def test_repo_gone_from_disk_retires_the_obligation_rather_than_ageing_it(self) -> None:
        """Inverts the original #3900 contract, on #4577's evidence.

        Ageing it was the design until sixteen of these reached ~12,000 drains
        apiece: ``ensure-pr`` cannot run in a directory that is not there, so the
        row could only ever end at a hand ``discharge-pending``.
        """
        PendingPullRequest.objects.owe(
            repo_path=str(self._tmp_path / "reaped-worktree"),
            branch="feat/vanished",
            reason="branch not on remote yet",
        )

        assert [signal.kind for signal in PendingPrDrainScanner().scan()] == ["pending_pr.retired"]

        assert not PendingPullRequest.objects.filter(branch="feat/vanished").exists()


class AnObligationWhoseCheckoutIsGoneRetiresItselfTestCase(TestCase):
    """The drain had no terminal state for "the checkout this row names is gone" (#4577).

    Sixteen such rows accumulated ~12,000 failed drains and sixteen permanent
    ``t3 doctor check`` FAILs, crowding out the five real findings underneath.
    """

    @pytest.fixture(autouse=True)
    def _inject_fixtures(self, tmp_path: Path) -> None:
        self._tmp_path = tmp_path

    def test_a_deleted_checkout_retires_its_obligation_and_says_so_once(self) -> None:
        _origin, repo, branch = _first_push_repo(self._tmp_path)
        call_command("pr", "ensure-pr", repo=str(repo), branch=branch)
        shutil.rmtree(repo)

        signals = PendingPrDrainScanner().scan()

        assert [signal.kind for signal in signals] == ["pending_pr.retired"]
        assert signals[0].payload["retired"][0]["branch"] == branch
        assert not PendingPullRequest.objects.exists()
        assert PendingPrDrainScanner().scan() == []

    def test_a_live_checkout_with_unpushed_work_still_owes_and_still_nags(self) -> None:
        """The load-bearing case: the invariant exists so real stranded work is never dropped."""
        _origin, repo, branch = _first_push_repo(self._tmp_path)
        call_command("pr", "ensure-pr", repo=str(repo), branch=branch)

        assert PendingPrDrainScanner().scan() == []

        owed = PendingPullRequest.objects.get(branch=branch)
        assert owed.drain_attempts == 1
        assert "not on remote yet" in owed.last_error

        PendingPullRequest.objects.filter(pk=owed.pk).update(drain_attempts=MAX_DRAIN_ATTEMPTS)
        assert check_pending_pull_requests() is False
