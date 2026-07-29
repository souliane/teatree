"""An ``ensure-pr`` deferral must leave a durable, drainable obligation.

``ensure-pr`` runs in the git PRE-push hook. On a first push the branch is not
on the remote yet, so the PR create is deferred — and git has no client-side
post-push hook to re-run it, so the deferral had no drain at all: exit 0,
"Passed", nothing stored, branch shipped with no PR.

Real git under ``tmp_path`` (a bare origin plus a clone) so the deferral is the
genuine one — the branch is absent from the remote because it was never pushed,
not because a classifier was patched to say so.
"""

from pathlib import Path
from typing import cast
from unittest.mock import MagicMock, patch

import pytest
from django.core.management import call_command
from django.test import TestCase

from teatree.core.management.commands import _ensure_pr as ensure_pr_mod
from teatree.core.models import PendingPullRequest
from teatree.utils.run import CommandFailedError
from tests.teatree_core.cleanup._shared import _run_git
from tests.teatree_core.pr_command._shared import _MOCK_OVERLAY


def _first_push_repo(tmp_path: Path) -> tuple[Path, str]:
    """A clone whose ``feat/orphan`` branch carries work git has never pushed."""
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
    return repo, "feat/orphan"


class EnsurePrDeferralIsAnObligationTestCase(TestCase):
    @pytest.fixture(autouse=True)
    def _inject_fixtures(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        self._tmp_path = tmp_path
        self._monkeypatch = monkeypatch

    def test_first_push_deferral_records_a_pending_pull_request(self) -> None:
        repo, branch = _first_push_repo(self._tmp_path)

        result = cast("dict[str, object]", call_command("pr", "ensure-pr", repo=str(repo), branch=branch))

        assert "not on remote yet" in str(result["skipped"])
        assert result["owed"] is True
        owed = PendingPullRequest.objects.get(branch=branch)
        assert owed.repo_path == str(repo)
        assert owed.drain_attempts == 0

    def test_re_deferring_the_same_branch_owes_once(self) -> None:
        repo, branch = _first_push_repo(self._tmp_path)

        call_command("pr", "ensure-pr", repo=str(repo), branch=branch)
        call_command("pr", "ensure-pr", repo=str(repo), branch=branch)

        assert PendingPullRequest.objects.filter(branch=branch).count() == 1

    def test_pre_push_race_deferral_persists_the_computed_spec(self) -> None:
        """#792's stale-remote defer owes the PR spec ``create_or_defer_pr`` already built."""
        host = MagicMock()
        host.current_user.return_value = "souliane"
        host.create_pr.side_effect = CommandFailedError(
            cmd=["gh", "pr", "create"],
            returncode=1,
            stdout="",
            stderr="GraphQL: No commits between main and feat-q (createPullRequest)",
        )
        self._monkeypatch.setattr(ensure_pr_mod, "code_host_for_repo_from_overlay", lambda _repo_path: host)

        with (
            patch("teatree.core.overlay_loader._discover_overlays", return_value=_MOCK_OVERLAY),
            patch.object(ensure_pr_mod.git, "remote_url", return_value="git@github.com:souliane/teatree.git"),
            patch.object(ensure_pr_mod, "_branch_own_commit_message", return_value=("feat: cool thing", "body")),
        ):
            result = ensure_pr_mod.create_or_defer_pr("/repo/path", "feat-q")

        assert "pre-push race" in str(result["skipped"])
        assert result["owed"] is True
        owed = PendingPullRequest.objects.get(branch="feat-q")
        assert owed.spec["title"] == "feat: cool thing"
        assert owed.spec["repo"] == "souliane/teatree"
