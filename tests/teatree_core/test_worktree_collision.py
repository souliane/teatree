"""Filesystem-evidence double-dispatch guard at the provisioning seam (#2217).

The DB lease (#2104) is blind when the DB has no ticket for an issue — exactly
the race that left issue #2217 momentarily without a Ticket while two agents
could provision. This guard keys on the issue number and works from the
``<N>-*`` worktree directory naming alone, so it catches the collision with or
without a DB ticket. Both the loop-dispatch and hand-dispatch paths provision
through the same seam, so one check covers both.
"""

from pathlib import Path

from django.test import TestCase

from teatree.core.models import Session, Ticket, Worktree
from teatree.core.worktree_collision import find_foreign_issue_worktrees


class TestFindForeignIssueWorktrees(TestCase):
    def _make_dir(self, workspace: Path, name: str) -> Path:
        d = workspace / name
        d.mkdir(parents=True)
        return d

    def test_no_existing_dir_returns_empty(self) -> None:
        workspace = Path(self.settings_tmp())
        own = workspace / "2217-fix-the-thing"
        assert find_foreign_issue_worktrees("2217", own_path=own, workspace_dir=workspace) == []

    def test_foreign_dir_for_same_issue_is_reported(self) -> None:
        workspace = Path(self.settings_tmp())
        foreign = self._make_dir(workspace, "2217-someone-else-slug")
        own = workspace / "2217-my-slug"
        result = find_foreign_issue_worktrees("2217", own_path=own, workspace_dir=workspace)
        assert result == [foreign.resolve()]

    def test_own_existing_dir_is_excluded_idempotent(self) -> None:
        workspace = Path(self.settings_tmp())
        own = self._make_dir(workspace, "2217-my-slug")
        result = find_foreign_issue_worktrees("2217", own_path=own, workspace_dir=workspace)
        assert result == []

    def test_other_issue_dir_is_not_reported(self) -> None:
        workspace = Path(self.settings_tmp())
        self._make_dir(workspace, "2218-unrelated")
        own = workspace / "2217-my-slug"
        assert find_foreign_issue_worktrees("2217", own_path=own, workspace_dir=workspace) == []

    def test_issue_number_prefix_is_not_a_loose_substring_match(self) -> None:
        # 22170 must NOT collide with 2217 — the glob is "<N>-*", anchored on the
        # full number followed by a hyphen, so a longer number is distinct.
        workspace = Path(self.settings_tmp())
        self._make_dir(workspace, "22170-different-issue")
        own = workspace / "2217-my-slug"
        assert find_foreign_issue_worktrees("2217", own_path=own, workspace_dir=workspace) == []

    def test_worktree_row_under_foreign_issue_dir_is_reported(self) -> None:
        workspace = Path(self.settings_tmp())
        foreign_repo = workspace / "2217-other-slug" / "teatree"
        foreign_repo.mkdir(parents=True)
        ticket = Ticket.objects.create(overlay="test", issue_url="https://x/2217")
        Session.objects.create(ticket=ticket, agent_id="a")
        Worktree.objects.create(ticket=ticket, repo_path="teatree", extra={"worktree_path": str(foreign_repo)})
        own = workspace / "2217-my-slug"
        result = find_foreign_issue_worktrees("2217", own_path=own, workspace_dir=workspace)
        assert (workspace / "2217-other-slug").resolve() in result

    def test_worktree_row_under_own_issue_dir_is_excluded(self) -> None:
        # A row whose path is the ticket's OWN dir must not be reported — the
        # corroborating DB signal honours the same own_path exclusion as the glob.
        workspace = Path(self.settings_tmp())
        own = self._make_dir(workspace, "2217-my-slug")
        own_repo = own / "teatree"
        own_repo.mkdir()
        ticket = Ticket.objects.create(overlay="test", issue_url="https://x/2217")
        Session.objects.create(ticket=ticket, agent_id="a")
        Worktree.objects.create(ticket=ticket, repo_path="teatree", extra={"worktree_path": str(own_repo)})
        result = find_foreign_issue_worktrees("2217", own_path=own, workspace_dir=workspace)
        assert result == []

    def test_worktree_row_outside_workspace_is_ignored(self) -> None:
        # A row whose materialised path is not under workspace_dir at all yields
        # no issue dir (the `_issue_dir_root is None` branch) and is skipped.
        workspace = Path(self.settings_tmp())
        elsewhere = Path(self.settings_tmp()) / "2217-elsewhere" / "teatree"
        elsewhere.mkdir(parents=True)
        ticket = Ticket.objects.create(overlay="test", issue_url="https://x/2217")
        Session.objects.create(ticket=ticket, agent_id="a")
        Worktree.objects.create(ticket=ticket, repo_path="teatree", extra={"worktree_path": str(elsewhere)})
        own = workspace / "2217-my-slug"
        assert find_foreign_issue_worktrees("2217", own_path=own, workspace_dir=workspace) == []

    def test_worktree_row_with_empty_path_is_skipped(self) -> None:
        # `extra={"worktree_path": ""}` survives the isnull exclude but is not a
        # path; the empty-string guard skips it without raising.
        workspace = Path(self.settings_tmp())
        ticket = Ticket.objects.create(overlay="test", issue_url="https://x/2217")
        Session.objects.create(ticket=ticket, agent_id="a")
        Worktree.objects.create(ticket=ticket, repo_path="teatree", extra={"worktree_path": ""})
        own = workspace / "2217-my-slug"
        assert find_foreign_issue_worktrees("2217", own_path=own, workspace_dir=workspace) == []

    def test_missing_workspace_dir_only_consults_worktree_rows(self) -> None:
        # When workspace_dir does not exist on disk, the glob arm is skipped and
        # only the Worktree-row arm runs — exercising the `is_dir()` False branch.
        parent = Path(self.settings_tmp())
        workspace = parent / "does-not-exist"
        own = workspace / "2217-my-slug"
        assert find_foreign_issue_worktrees("2217", own_path=own, workspace_dir=workspace) == []

    def settings_tmp(self) -> str:
        # one tmp workspace per test method
        import tempfile  # noqa: PLC0415

        d = tempfile.mkdtemp(prefix="t3-collision-")
        self.addCleanup(self._rmtree, d)
        return d

    @staticmethod
    def _rmtree(path: str) -> None:
        import shutil  # noqa: PLC0415

        shutil.rmtree(path, ignore_errors=True)
