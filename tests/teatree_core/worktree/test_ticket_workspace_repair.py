# test-path: cross-cutting
"""The targeted split-workspace recovery (#111 finding 4, real git).

``workspace clean-all`` is the DONE-worktree reaper — it deliberately keeps an
unfinished checkout, so prescribing it for a still-open split ticket repairs
nothing. This is the recovery that actually does: it MOVES each divergent
checkout into the ticket's canonical dir, preserving both an unpushed commit
and an uncommitted change, rather than reaping anything.
"""

from pathlib import Path
from unittest.mock import patch

import pytest
from django.test import TestCase

from teatree.core.models import Ticket, Worktree
from teatree.core.worktree.ticket_workspace import ticket_workspace_dirs
from teatree.core.worktree.ticket_workspace_repair import repair_split_workspace
from teatree.utils import git

_BRANCH = "111-repair"


class TestRepairSplitWorkspace(TestCase):
    @pytest.fixture(autouse=True)
    def _tmp_workspace(self, tmp_path: Path) -> None:
        self.tmp = tmp_path
        self.workspace = tmp_path / "workspace"
        self.workspace.mkdir()

    def _clone(self, repo: str) -> Path:
        # A real, PUSHED origin: unpushed-work detection asks whether a commit is
        # absent from every remote, so a remote-less fixture would make even the
        # base commit look unpushed.
        origin = self.tmp / "origin" / f"{repo}.git"
        git.run_strict(repo=str(self.tmp), args=["init", "-q", "--bare", "-b", "main", str(origin)])
        clone = self.tmp / "clones" / repo
        clone.mkdir(parents=True)
        git.run_strict(repo=str(clone), args=["init", "-q", "-b", "main"])
        git.run_strict(repo=str(clone), args=["config", "user.email", "t@example.com"])
        git.run_strict(repo=str(clone), args=["config", "user.name", "t"])
        git.run_strict(repo=str(clone), args=["remote", "add", "origin", str(origin)])
        (clone / "README.md").write_text("x\n", encoding="utf-8")
        git.run_strict(repo=str(clone), args=["add", "-A"])
        git.run_strict(repo=str(clone), args=["commit", "-q", "-m", "init"])
        git.run_strict(repo=str(clone), args=["push", "-q", "-u", "origin", "main"])
        return clone

    def _add_worktree(self, clone: Path, path: Path, branch: str) -> Path:
        path.parent.mkdir(parents=True, exist_ok=True)
        git.run_strict(repo=str(clone), args=["worktree", "add", "-q", "-b", branch, str(path)])
        return path

    def test_repairs_a_split_unfinished_ticket_preserving_unpushed_and_uncommitted_work(self) -> None:
        backend_clone = self._clone("backend")
        frontend_clone = self._clone("frontend")

        canonical = self.workspace / _BRANCH
        backend = self._add_worktree(backend_clone, canonical / "backend", _BRANCH)

        foreign_root = self.tmp / "foreign-root"
        frontend = self._add_worktree(frontend_clone, foreign_root / "frontend", _BRANCH)
        # Unfinished, real work: one unpushed commit AND one uncommitted change.
        (frontend / "feature.py").write_text("unpushed = True\n", encoding="utf-8")
        git.run_strict(repo=str(frontend), args=["add", "-A"])
        git.run_strict(repo=str(frontend), args=["commit", "-q", "-m", "unpushed work"])
        (frontend / "wip.py").write_text("uncommitted = True\n", encoding="utf-8")

        ticket = Ticket.objects.create(
            overlay="test",
            issue_url="https://example.com/issues/111-repair",
            repos=["backend", "frontend"],
            extra={"branch": _BRANCH},
        )
        Worktree.objects.create(
            ticket=ticket, overlay="test", repo_path="backend", branch=_BRANCH, extra={"worktree_path": str(backend)}
        )
        Worktree.objects.create(
            ticket=ticket,
            overlay="test",
            repo_path="frontend",
            branch=_BRANCH,
            extra={"worktree_path": str(frontend)},
        )
        assert len(ticket_workspace_dirs(ticket)) == 2, "fixture must start genuinely split"

        with patch("teatree.core.worktree.ticket_workspace_repair.worktree_root", return_value=self.workspace):
            outcome = repair_split_workspace(ticket)

        assert outcome.ok, outcome.render()
        assert outcome.moved == ["frontend"]
        assert outcome.kept == ["backend"]
        assert len(ticket_workspace_dirs(ticket)) == 1, "the split must be actually repaired"

        moved_row = Worktree.objects.get(ticket=ticket, repo_path="frontend")
        new_path = Path(moved_row.extra["worktree_path"])
        assert new_path.parent == canonical, "frontend must have moved INTO the canonical dir"
        assert (new_path / "feature.py").read_text(encoding="utf-8") == "unpushed = True\n", "unpushed commit lost"
        assert (new_path / "wip.py").read_text(encoding="utf-8") == "uncommitted = True\n", "uncommitted work lost"
        assert git.current_branch(str(new_path)) == _BRANCH
        assert not frontend.exists(), "the old foreign-root checkout must be gone, not duplicated"

    def test_a_ticket_with_one_workspace_dir_is_a_no_op(self) -> None:
        backend_clone = self._clone("backend")
        canonical = self.workspace / _BRANCH
        backend = self._add_worktree(backend_clone, canonical / "backend", _BRANCH)
        ticket = Ticket.objects.create(
            overlay="test",
            issue_url="https://example.com/issues/111-noop",
            repos=["backend"],
            extra={"branch": _BRANCH},
        )
        Worktree.objects.create(
            ticket=ticket, overlay="test", repo_path="backend", branch=_BRANCH, extra={"worktree_path": str(backend)}
        )

        with patch("teatree.core.worktree.ticket_workspace_repair.worktree_root", return_value=self.workspace):
            outcome = repair_split_workspace(ticket)

        assert outcome.ok
        assert outcome.moved == []
        assert outcome.kept == []
