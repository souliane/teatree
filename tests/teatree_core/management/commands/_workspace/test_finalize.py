"""``workspace finalize`` squashes and rebases each worktree inside its own checkout."""

import os
from io import StringIO
from pathlib import Path
from typing import cast
from unittest.mock import patch

import pytest
from django.core.management import call_command
from django.test import TestCase

from teatree.core.models import Ticket, Worktree
from tests._git_repo import git_identity_env, make_git_repo, run_git


class TestFinalizeReadsTheCheckout(TestCase):
    @pytest.fixture(autouse=True)
    def _inject(self, tmp_path: Path) -> None:
        self._tmp = tmp_path

    def _worktree_with_two_commits(self) -> Path:
        remote = make_git_repo(self._tmp / "origin.git", bare=True)
        clone = make_git_repo(self._tmp / "clone")
        run_git(clone, "remote", "add", "origin", str(remote))
        run_git(clone, "push", "-q", "origin", "HEAD:main")
        run_git(clone, "fetch", "-q", "origin")
        checkout = self._tmp / "wt"
        run_git(clone, "worktree", "add", "-q", "-b", "feature-x", str(checkout))
        for name in ("a.py", "b.py"):
            (checkout / name).write_text("x = 1\n", encoding="utf-8")
            run_git(checkout, "add", name)
            run_git(checkout, "commit", "-q", "-m", f"add {name}")
        return checkout

    def test_a_slug_repo_path_finalizes_in_the_recorded_checkout(self) -> None:
        checkout = self._worktree_with_two_commits()
        ticket = Ticket.objects.create(overlay="test", issue_url="https://github.com/souliane/teatree/issues/5145")
        Worktree.objects.create(
            overlay="test",
            ticket=ticket,
            repo_path="souliane/teatree",
            branch="feature-x",
            extra={"worktree_path": str(checkout)},
        )

        with patch.dict(os.environ, git_identity_env()):
            report = cast("str", call_command("workspace", "finalize", str(ticket.pk), stdout=StringIO()))

        assert "souliane/teatree: squashed 2 commits" in report
        assert "souliane/teatree: rebased on main" in report
        assert run_git(checkout, "rev-list", "--count", "origin/main..HEAD") == "1"
