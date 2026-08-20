"""``PendingPrDrainScanner`` — the drain the pre-push deferral never had.

Real git under ``tmp_path`` throughout: the obligation is created by the real
``ensure-pr`` deferral, and the drain's verdict is decided by the real branch
state on a real remote, not by a patched classifier.
"""

from pathlib import Path

import pytest
from django.core.management import call_command
from django.test import TestCase

from teatree.core.models import PendingPullRequest
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
    @pytest.fixture(autouse=True)
    def _inject_fixtures(self, tmp_path: Path) -> None:
        self._tmp_path = tmp_path

    def test_no_obligations_emits_nothing(self) -> None:
        assert PendingPrDrainScanner().scan() == []

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

    def test_repo_gone_from_disk_keeps_the_obligation_rather_than_dropping_it(self) -> None:
        PendingPullRequest.objects.owe(
            repo_path=str(self._tmp_path / "reaped-worktree"),
            branch="feat/vanished",
            reason="branch not on remote yet",
        )

        assert PendingPrDrainScanner().scan() == []

        owed = PendingPullRequest.objects.get(branch="feat/vanished")
        assert owed.drain_attempts == 1
