"""Per-worktree git checks — the #F1.4 transient-probe-vs-verdict distinction.

``worktree_has_commits_ahead`` feeds ``has_shippable_diff`` → ``review()`` →
``dispose_unshippable_review()`` → ``ticket.ignore()``. A transient git failure
that silently returns ``False`` there terminally ABANDONS a live ticket. The fix:
a genuinely-missing path/branch is an honest ``False`` (nothing to ship), but a
PRESENT checkout whose probe FAILS raises ``WorktreeProbeUnverifiableError`` so
the caller can hold/skip the tick instead of disposing.
"""

from pathlib import Path
from unittest.mock import patch

import pytest
from django.test import TestCase

from teatree.core.models import Ticket, Worktree
from teatree.core.models.ticket_worktree_checks import (
    WorktreeProbeUnverifiableError,
    _resolve_base_branch,
    worktree_has_commits_ahead,
)
from teatree.utils import git as git_mod
from teatree.utils.run import CommandFailedError
from tests.teatree_core.models._shared import _init_repo_with_branch


class TestWorktreeHasCommitsAhead(TestCase):
    @pytest.fixture(autouse=True)
    def _inject_tmp_path(self, tmp_path: Path) -> None:
        self._tmp_path = tmp_path

    def _worktree(self, *, repo_path: str, branch: str) -> Worktree:
        ticket = Ticket.objects.create(role=Ticket.Role.AUTHOR, state=Ticket.State.TESTED)
        return Worktree.objects.create(
            ticket=ticket,
            repo_path=repo_path,
            branch=branch,
            extra={"worktree_path": repo_path} if repo_path else {},
        )

    def test_true_when_branch_has_commits_ahead(self) -> None:
        repo = self._tmp_path / "repo-ahead"
        _init_repo_with_branch(repo, branch="feature", commits_ahead=2)
        wt = self._worktree(repo_path=str(repo), branch="feature")
        assert worktree_has_commits_ahead(wt) is True

    def test_false_when_branch_has_no_commits_ahead(self) -> None:
        repo = self._tmp_path / "repo-level"
        _init_repo_with_branch(repo, branch="feature", commits_ahead=0)
        wt = self._worktree(repo_path=str(repo), branch="feature")
        assert worktree_has_commits_ahead(wt) is False

    def test_false_when_no_recorded_path_or_branch(self) -> None:
        wt = self._worktree(repo_path="", branch="")
        assert worktree_has_commits_ahead(wt) is False

    def test_false_when_recorded_checkout_is_gone_from_disk(self) -> None:
        # Genuinely-missing checkout — nothing on disk to ship. Honest False, NOT
        # a probe failure (safe to dispose).
        wt = self._worktree(repo_path=str(self._tmp_path / "never-existed"), branch="feature")
        assert worktree_has_commits_ahead(wt) is False

    def test_raises_when_present_checkout_probe_fails(self) -> None:
        # A PRESENT on-disk checkout whose commit-count probe FAILS must NOT be
        # flattened to False (which would route the ticket to terminal ignore()).
        repo = self._tmp_path / "repo-probe-fail"
        _init_repo_with_branch(repo, branch="feature", commits_ahead=1)
        wt = self._worktree(repo_path=str(repo), branch="feature")

        boom = CommandFailedError(["git", "rev-list"], 128, "", "fatal: bad revision")
        with (
            patch.object(git_mod, "rev_count", side_effect=boom),
            pytest.raises(WorktreeProbeUnverifiableError),
        ):
            worktree_has_commits_ahead(wt)


class TestResolveBaseBranch(TestCase):
    @pytest.fixture(autouse=True)
    def _inject_tmp_path(self, tmp_path: Path) -> None:
        self._tmp_path = tmp_path

    def test_falls_back_to_local_main_without_origin(self) -> None:
        # A repo with no origin remote resolves to the local "main" default.
        repo = self._tmp_path / "no-origin"
        _init_repo_with_branch(repo, branch="feature", commits_ahead=1)
        assert _resolve_base_branch(str(repo)) == "main"
