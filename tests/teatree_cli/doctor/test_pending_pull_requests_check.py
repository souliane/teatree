"""``check_pending_pull_requests`` — a PR owed since push must fail loud, not retry in silence."""

from pathlib import Path
from unittest.mock import patch

import django.test
import pytest

from teatree.cli.doctor.checks_pending_pr import check_pending_pull_requests
from teatree.core.models import PendingPullRequest
from teatree.core.models.pending_pull_request import MAX_DRAIN_ATTEMPTS
from tests._git_repo import make_git_repo, run_git
from tests.factories import serialized_pr_spec


class PendingPullRequestDoctorCheckTestCase(django.test.TestCase):
    @pytest.fixture(autouse=True)
    def _inject_fixtures(self, capsys: pytest.CaptureFixture[str], tmp_path: Path) -> None:
        self._capsys = capsys
        self._tmp_path = tmp_path

    def _owed(self, *, attempts: int) -> PendingPullRequest:
        row = PendingPullRequest.objects.owe(
            repo_path=str(self._tmp_path),
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


class PendingPullRequestBehindABaseRefactorTestCase(django.test.TestCase):
    """#3977: a branch a real merge would conflict with needs a person, not a retry.

    The stale branch modifies a file the base independently deletes since the
    fork — the canonical "predates a refactor" shape a real merge conflicts on
    (#3977 review: a two-dot line-count heuristic would ALSO flag an ordinary
    branch that is merely behind an active, unrelated-churn base — this fixture
    is deliberately the genuine-conflict case, not that false-positive shape).
    """

    @pytest.fixture(autouse=True)
    def _inject_fixtures(self, capsys: pytest.CaptureFixture[str], tmp_path: Path) -> None:
        self._capsys = capsys
        self._tmp_path = tmp_path

    def _stale_branch_repo(self) -> Path:
        repo = make_git_repo(self._tmp_path / "clone")
        (repo / "app").mkdir()
        (repo / "app" / "parse.py").write_text("def parse(x):\n    return 0\n")
        run_git(repo, "add", "-A")
        run_git(repo, "commit", "-q", "-m", "initial")
        run_git(repo, "checkout", "-q", "-b", "feat/stale")
        (repo / "app" / "parse.py").write_text("def parse(x):\n    return int(x)\n")
        run_git(repo, "add", "-A")
        run_git(repo, "commit", "-q", "-m", "feat: the feature")
        run_git(repo, "checkout", "-q", "main")
        (repo / "app" / "parse.py").unlink()
        run_git(repo, "add", "-A")
        run_git(repo, "commit", "-q", "-m", "refactor: remove the module the branch predates")
        run_git(repo, "update-ref", "refs/remotes/origin/main", "main")
        return repo

    def _owe_undrainable(self, repo: Path) -> None:
        row = PendingPullRequest.objects.owe(
            repo_path=str(repo),
            branch="feat/stale",
            reason="branch not on remote yet",
            spec=serialized_pr_spec("feat: the feature"),
        )
        PendingPullRequest.objects.filter(pk=row.pk).update(drain_attempts=MAX_DRAIN_ATTEMPTS)

    def test_remedy_names_the_revert_risk_instead_of_urging_a_pr(self) -> None:
        self._owe_undrainable(self._stale_branch_repo())

        assert check_pending_pull_requests() is False

        out = self._capsys.readouterr().out
        assert "FAIL" in out
        assert "feat/stale" in out
        assert "revert" in out.lower()
        assert "rebase" in out.lower()
        assert "app/parse.py" in out
        assert "pr ensure-pr" not in out

    def test_an_ordinary_behind_branch_still_gets_the_actionable_remedy(self) -> None:
        """#3977 review: a branch merely behind an active base is NOT at risk — no false positive.

        Only ``unrelated.py`` diverges; nothing the branch touched conflicts
        with the base's independent progress, so a real merge is clean and the
        operator must still see the actionable ``pr ensure-pr`` instruction.
        """
        repo = make_git_repo(self._tmp_path / "clone")
        (repo / "app.py").write_text("core = 1\n")
        run_git(repo, "add", "-A")
        run_git(repo, "commit", "-q", "-m", "base")
        run_git(repo, "checkout", "-q", "-b", "feat/behind")
        (repo / "newfeature.py").write_text("def feature():\n    return 42\n")
        run_git(repo, "add", "-A")
        run_git(repo, "commit", "-q", "-m", "feat: real work")
        run_git(repo, "checkout", "-q", "main")
        (repo / "unrelated.py").write_text("".join(f"line {n}\n" for n in range(300)))
        run_git(repo, "add", "-A")
        run_git(repo, "commit", "-q", "-m", "feat: ordinary churn on an active base")
        run_git(repo, "update-ref", "refs/remotes/origin/main", "main")
        row = PendingPullRequest.objects.owe(
            repo_path=str(repo),
            branch="feat/behind",
            reason="branch not on remote yet",
        )
        PendingPullRequest.objects.filter(pk=row.pk).update(drain_attempts=MAX_DRAIN_ATTEMPTS)

        assert check_pending_pull_requests() is False

        out = self._capsys.readouterr().out
        assert "pr ensure-pr" in out
        assert "REVERT" not in out

    def test_a_probe_that_blows_up_still_reports_the_obligation(self) -> None:
        """The doctor owns its own crash-safety — nothing here may abort the run."""
        self._owe_undrainable(self._stale_branch_repo())

        with patch(
            "teatree.cli.doctor.checks_pending_pr.assess_revert_risk",
            side_effect=OSError("git is not on PATH"),
        ):
            assert check_pending_pull_requests() is False

        assert "pr ensure-pr" in self._capsys.readouterr().out

    def test_unreadable_repo_keeps_the_plain_remedy_and_never_crashes(self) -> None:
        """A directory that is present but is no repository — the probe fails, the remedy stands."""
        unreadable = self._tmp_path / "not-a-repo"
        unreadable.mkdir()
        row = PendingPullRequest.objects.owe(
            repo_path=str(unreadable),
            branch="feat/stale",
            reason="branch not on remote yet",
        )
        PendingPullRequest.objects.filter(pk=row.pk).update(drain_attempts=MAX_DRAIN_ATTEMPTS)

        assert check_pending_pull_requests() is False

        out = self._capsys.readouterr().out
        assert "pr ensure-pr" in out

    def test_an_absent_checkout_gets_a_remedy_that_can_terminate(self) -> None:
        """#4577: "open a PR" is unrunnable once the checkout is gone, so it FAILs forever."""
        row = PendingPullRequest.objects.owe(
            repo_path=str(self._tmp_path / "gone"),
            branch="feat/stale",
            reason="branch not on remote yet",
        )
        PendingPullRequest.objects.filter(pk=row.pk).update(drain_attempts=MAX_DRAIN_ATTEMPTS)

        assert check_pending_pull_requests() is False

        out = self._capsys.readouterr().out
        assert "checkout is gone" in out
        assert f"pr discharge-pending {row.pk}" in out
        assert "pr ensure-pr" not in out
