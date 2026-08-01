# test-path: cross-cutting
"""Same ticket → same workspace: the registration-time refusal (real git).

Integration-first over a real ``git`` clone + linked worktrees under ``tmp_path``,
because the two things under test are both things only real git answers: which
clone a checkout belongs to (``rev-parse --git-common-dir``, the basis of
``repo_path``) and whether a second checkout may join a ticket that already has a
workspace.

The refusal is the load-bearing assertion. A ticket whose repos are split across
two parent dirs cannot resolve its own siblings, so the generated stack silently
loses services and provisioning still reports success — the failure mode that made
this a hard refusal at registration instead of a log warning.
"""

import subprocess
import tempfile
from pathlib import Path

import pytest
from django.test import TestCase

from teatree.core.models import Ticket, Worktree
from teatree.core.provision.worktree_adopt import WorktreeAdoptError, adopt_worktree_for_ticket
from teatree.core.worktree.ticket_workspace import (
    TicketWorkspaceDivergenceError,
    assert_joins_ticket_workspace,
    ticket_workspace_dir,
)
from tests.teatree_core.cleanup._shared import _clean_env, _run_git


class _TicketWorkspaceCase(TestCase):
    """A real clone with two repos, and a ticket whose workspace holds one of them."""

    def setUp(self) -> None:
        super().setUp()
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(lambda: subprocess.run(["/bin/rm", "-rf", str(self.tmp)], check=False, env=_clean_env()))
        self.ticket = Ticket.objects.create(overlay="test", state=Ticket.State.MERGED)
        self.workspace = self.tmp / "t3-workspaces" / "test" / "42-ticket"
        self.backend_clone = self._clone("backend")
        self.frontend_clone = self._clone("frontend")

    def _clone(self, name: str) -> Path:
        clone = self.tmp / "clones" / name
        clone.mkdir(parents=True)
        _run_git("init", "-q", "-b", "main", cwd=clone)
        _run_git("config", "user.email", "t@t", cwd=clone)
        _run_git("config", "user.name", "t", cwd=clone)
        _run_git("commit", "--allow-empty", "-q", "-m", "init", cwd=clone)
        return clone

    def _add_worktree(self, clone: Path, path: Path, branch: str) -> Path:
        path.parent.mkdir(parents=True, exist_ok=True)
        _run_git("worktree", "add", "-q", "-b", branch, str(path), cwd=clone)
        return path

    def _register(self, repo: str, path: Path, branch: str) -> Worktree:
        return Worktree.objects.create(
            ticket=self.ticket, overlay="test", repo_path=repo, branch=branch, extra={"worktree_path": str(path)}
        )


class TestTicketWorkspaceDir(_TicketWorkspaceCase):
    def test_none_when_ticket_has_no_materialised_worktree(self) -> None:
        # Nothing to join yet — the first provision must not be refused.
        assert ticket_workspace_dir(self.ticket) is None

    def test_resolves_the_shared_parent_of_the_ticket_rows(self) -> None:
        wt = self._add_worktree(self.backend_clone, self.workspace / "backend", "42-be")
        self._register("backend", wt, "42-be")

        assert ticket_workspace_dir(self.ticket) == self.workspace

    def test_none_when_existing_rows_already_disagree(self) -> None:
        # A pre-existing split has no single workspace to join, so the predicate
        # declines to pick a winner and the refusal stays a no-op rather than
        # hard-failing an unrelated call site. Draining these is the reaper's job.
        be = self._add_worktree(self.backend_clone, self.tmp / "root-a" / "backend", "42-be")
        fe = self._add_worktree(self.frontend_clone, self.tmp / "root-b" / "frontend", "42-fe")
        self._register("backend", be, "42-be")
        self._register("frontend", fe, "42-fe")

        assert ticket_workspace_dir(self.ticket) is None

    def test_ignores_a_row_whose_directory_is_gone(self) -> None:
        # A torn-down worktree's stale row must not pin the ticket to a dead dir.
        wt = self._add_worktree(self.backend_clone, self.workspace / "backend", "42-be")
        self._register("backend", wt, "42-be")
        self._register("reports", self.tmp / "vanished" / "reports", "42-rep")

        assert ticket_workspace_dir(self.ticket) == self.workspace


class TestAssertJoinsTicketWorkspace(_TicketWorkspaceCase):
    def test_accepts_a_sibling_in_the_established_workspace(self) -> None:
        be = self._add_worktree(self.backend_clone, self.workspace / "backend", "42-be")
        self._register("backend", be, "42-be")
        fe = self._add_worktree(self.frontend_clone, self.workspace / "frontend", "42-fe")

        assert_joins_ticket_workspace(self.ticket, fe)  # no raise

    def test_refuses_a_checkout_from_a_foreign_root(self) -> None:
        be = self._add_worktree(self.backend_clone, self.workspace / "backend", "42-be")
        self._register("backend", be, "42-be")
        elsewhere = self._add_worktree(self.frontend_clone, self.tmp / "t3-workspaces" / "frontend" / "42-fe", "42-fe")

        with pytest.raises(TicketWorkspaceDivergenceError) as exc:
            assert_joins_ticket_workspace(self.ticket, elsewhere)
        # The message must name both dirs — the operator's next action is a move,
        # which is not guessable from "divergent path".
        assert str(self.workspace) in str(exc.value)
        assert str(elsewhere) in str(exc.value)


class TestAdoptEnforcesTheInvariant(_TicketWorkspaceCase):
    """The refusal at the seam that produced the real split, plus the repo_path fix."""

    def test_adoption_from_a_foreign_root_is_refused_and_writes_no_row(self) -> None:
        be = self._add_worktree(self.backend_clone, self.workspace / "backend", "42-be")
        self._register("backend", be, "42-be")
        elsewhere = self._add_worktree(self.frontend_clone, self.tmp / "t3-workspaces" / "frontend" / "42-fe", "42-fe")
        before = Worktree.objects.count()

        with pytest.raises(WorktreeAdoptError, match="must be siblings in ONE workspace dir"):
            adopt_worktree_for_ticket(self.ticket, cwd=str(elsewhere))

        assert Worktree.objects.count() == before

    def test_adoption_into_the_ticket_workspace_records_the_repo_not_the_dir_name(self) -> None:
        be = self._add_worktree(self.backend_clone, self.workspace / "backend", "42-be")
        self._register("backend", be, "42-be")
        # A hand-made `git worktree add` names the dir after the BRANCH, so the
        # directory basename is not the repo — `repo_path` must still be the repo.
        fe = self._add_worktree(self.frontend_clone, self.workspace / "42-fe-branch-named-dir", "42-fe")

        row = adopt_worktree_for_ticket(self.ticket, cwd=str(fe))

        assert row.repo_path == "frontend"
        assert row.extra["worktree_path"] == str(fe.resolve())
