"""A dispatched agent starts in the ticket's materialised worktree, read from ``extra['worktree_path']`` (#5151)."""

import tempfile
from pathlib import Path

from django.test import TestCase

from teatree.agents._runner_options import _build_options, _resolve_task_cwd
from teatree.core.models import Session, Task, Worktree
from tests.factories import planned_ticket


class TestTaskCwdComesFromTheWorktreePath(TestCase):
    def setUp(self) -> None:
        self.checkout = self.enterContext(tempfile.TemporaryDirectory())
        ticket = planned_ticket()
        self.task = Task.objects.create(ticket=ticket, session=Session.objects.create(ticket=ticket))
        self.ticket = ticket

    def _worktree(self, *, repo_path: str, extra: dict[str, str]) -> None:
        Worktree.objects.create(ticket=self.ticket, overlay="t3-teatree", repo_path=repo_path, extra=extra)

    def test_a_live_worktree_row_resolves_to_its_checkout(self) -> None:
        self._worktree(repo_path="org/repo", extra={"worktree_path": self.checkout})

        assert _resolve_task_cwd(self.task) == self.checkout

    def test_claude_options_start_in_that_checkout(self) -> None:
        self._worktree(repo_path="org/repo", extra={"worktree_path": self.checkout})

        options = _build_options(self.task, "context", phase="coding", skills=[])

        assert options.cwd == self.checkout
        assert self.checkout in [str(path) for path in options.add_dirs]

    def test_a_legacy_row_whose_repo_path_is_a_directory_still_resolves(self) -> None:
        self._worktree(repo_path=self.checkout, extra={})

        assert _resolve_task_cwd(self.task) == self.checkout

    def test_the_recorded_worktree_path_wins_over_a_path_valued_repo_path(self) -> None:
        with tempfile.TemporaryDirectory() as legacy:
            self._worktree(repo_path=legacy, extra={"worktree_path": self.checkout})

            assert _resolve_task_cwd(self.task) == self.checkout

    def test_an_identifier_that_is_no_directory_leaves_the_cwd_unset(self) -> None:
        self._worktree(repo_path="org/repo", extra={})

        assert _resolve_task_cwd(self.task) is None

    def test_a_recorded_path_that_is_gone_leaves_the_cwd_unset(self) -> None:
        self._worktree(repo_path="org/repo", extra={"worktree_path": str(Path(self.checkout) / "gone")})

        assert _resolve_task_cwd(self.task) is None
