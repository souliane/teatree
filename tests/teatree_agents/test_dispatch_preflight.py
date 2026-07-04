"""Dispatch preflight head-state resolution + maker-brief block (PR-12)."""

import tempfile
from datetime import UTC, datetime, timedelta
from pathlib import Path

from django.test import TestCase

from teatree.agents.dispatch_preflight import head_state_brief_lines, resolve_head_state
from teatree.core.models import Session, Task, Ticket, Worktree
from tests._git_repo import make_git_repo, run_git


def _worktree_with_commit(tmp: Path, *, subject: str) -> str:
    make_git_repo(tmp, default_branch="feat-x")
    (tmp / "f.txt").write_text("x\n")
    run_git(tmp, "add", "f.txt")
    run_git(tmp, "commit", "-q", "-m", subject)
    return str(tmp)


class TestResolveHeadState(TestCase):
    def test_none_when_ticket_has_no_worktree(self) -> None:
        ticket = Ticket.objects.create(issue_url="https://example.com/issues/1")
        session = Session.objects.create(ticket=ticket)
        task = Task.objects.create(ticket=ticket, session=session, phase="coding")
        assert resolve_head_state(task) is None

    def test_reads_head_commit_of_the_ticket_worktree(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = _worktree_with_commit(Path(tmp), subject="feat: land the thing")
            ticket = Ticket.objects.create(issue_url="https://example.com/issues/2")
            Worktree.objects.create(ticket=ticket, repo_path=path, branch="feat-x", extra={"worktree_path": path})
            session = Session.objects.create(ticket=ticket)
            task = Task.objects.create(ticket=ticket, session=session, phase="coding")

            state = resolve_head_state(task)
            assert state is not None
            assert state.subject == "feat: land the thing"
            assert state.branch == "feat-x"
            assert len(state.sha) == 40
            assert state.committed_at is not None

    def test_none_when_worktree_path_is_not_a_git_repo(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            ticket = Ticket.objects.create(issue_url="https://example.com/issues/3")
            Worktree.objects.create(ticket=ticket, repo_path=tmp, branch="b", extra={"worktree_path": tmp})
            session = Session.objects.create(ticket=ticket)
            task = Task.objects.create(ticket=ticket, session=session, phase="coding")
            assert resolve_head_state(task) is None


class TestHeadStateBriefLines(TestCase):
    def test_empty_when_no_head_state(self) -> None:
        ticket = Ticket.objects.create(issue_url="https://example.com/issues/4")
        session = Session.objects.create(ticket=ticket)
        task = Task.objects.create(ticket=ticket, session=session, phase="coding")
        assert head_state_brief_lines(task) == ()

    def test_block_carries_sha_subject_and_build_on_directive(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = _worktree_with_commit(Path(tmp), subject="feat: partial work")
            ticket = Ticket.objects.create(issue_url="https://example.com/issues/5")
            Worktree.objects.create(ticket=ticket, repo_path=path, branch="feat-x", extra={"worktree_path": path})
            session = Session.objects.create(ticket=ticket)
            task = Task.objects.create(ticket=ticket, session=session, phase="coding")

            block = "\n".join(head_state_brief_lines(task))
            assert "DISPATCH PREFLIGHT" in block
            assert "feat: partial work" in block
            assert "do NOT restart" in block

    def test_flags_commit_landed_after_trigger(self) -> None:
        # The commit is "now"; force the task's trigger timestamp to the past so
        # the commit is unambiguously after it — the maker is told work already
        # landed in this cycle.
        with tempfile.TemporaryDirectory() as tmp:
            path = _worktree_with_commit(Path(tmp), subject="feat: in-cycle commit")
            ticket = Ticket.objects.create(issue_url="https://example.com/issues/6")
            Worktree.objects.create(ticket=ticket, repo_path=path, branch="feat-x", extra={"worktree_path": path})
            session = Session.objects.create(ticket=ticket)
            task = Task.objects.create(ticket=ticket, session=session, phase="coding")
            Task.objects.filter(pk=task.pk).update(created_at=datetime.now(tz=UTC) - timedelta(days=1))
            task.refresh_from_db()

            block = "\n".join(head_state_brief_lines(task))
            assert "landed AFTER this dispatch" in block

    def test_flags_commit_predates_trigger(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = _worktree_with_commit(Path(tmp), subject="feat: old commit")
            ticket = Ticket.objects.create(issue_url="https://example.com/issues/7")
            Worktree.objects.create(ticket=ticket, repo_path=path, branch="feat-x", extra={"worktree_path": path})
            session = Session.objects.create(ticket=ticket)
            task = Task.objects.create(ticket=ticket, session=session, phase="coding")
            Task.objects.filter(pk=task.pk).update(created_at=datetime.now(tz=UTC) + timedelta(days=1))
            task.refresh_from_db()

            block = "\n".join(head_state_brief_lines(task))
            assert "HEAD predates this dispatch" in block
