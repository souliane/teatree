"""Dispatch preflight head-state resolution + maker-brief block (PR-12), branch currency (#2663)."""

import shutil
import tempfile
from datetime import UTC, datetime, timedelta
from pathlib import Path

from django.test import TestCase

from teatree.agents.dispatch_preflight import (
    branch_currency_brief_lines,
    head_state_brief_lines,
    resolve_head_state,
    review_diff_brief_lines,
)
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


def _reviewing_worktree(tmp: Path) -> str:
    make_git_repo(tmp, default_branch="main")
    run_git(tmp, "update-ref", "refs/remotes/origin/main", "HEAD")
    run_git(tmp, "checkout", "-q", "-b", "feature")
    (tmp / "widget.py").write_text("def widget() -> int:\n    return 7\n")
    run_git(tmp, "add", "widget.py")
    run_git(tmp, "commit", "-q", "-m", "feat: add widget")
    return str(tmp)


class TestReviewDiffBriefLines(TestCase):
    def test_empty_when_ticket_has_no_worktree(self) -> None:
        ticket = Ticket.objects.create(issue_url="https://example.com/issues/8")
        session = Session.objects.create(ticket=ticket)
        task = Task.objects.create(ticket=ticket, session=session, phase="reviewing")
        assert review_diff_brief_lines(task) == ()

    def test_empty_when_worktree_path_is_not_a_git_repo(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            ticket = Ticket.objects.create(issue_url="https://example.com/issues/9")
            Worktree.objects.create(ticket=ticket, repo_path=tmp, branch="b", extra={"worktree_path": tmp})
            session = Session.objects.create(ticket=ticket)
            task = Task.objects.create(ticket=ticket, session=session, phase="reviewing")
            assert review_diff_brief_lines(task) == ()

    def test_block_carries_the_branch_diff_for_a_shell_denied_reviewer(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = _reviewing_worktree(Path(tmp))
            ticket = Ticket.objects.create(issue_url="https://example.com/issues/10")
            Worktree.objects.create(ticket=ticket, repo_path=path, branch="feature", extra={"worktree_path": path})
            session = Session.objects.create(ticket=ticket)
            task = Task.objects.create(ticket=ticket, session=session, phase="reviewing")

            block = "\n".join(review_diff_brief_lines(task))
            assert "DIFF UNDER REVIEW" in block
            assert "widget.py" in block
            assert "def widget" in block


def _remote_and_clone(root: Path) -> tuple[Path, Path]:
    """A bare ``origin`` holding ``main``, plus a clone checked out on a feature branch."""
    seed = make_git_repo(root / "seed", default_branch="main")
    (seed / "a.txt").write_text("base\n")
    run_git(seed, "add", "a.txt")
    run_git(seed, "commit", "-q", "-m", "seed a.txt")
    bare = root / "remote.git"
    run_git(root, "clone", "-q", "--bare", str(seed), str(bare))
    clone = root / "clone"
    run_git(root, "clone", "-q", str(bare), str(clone))
    run_git(clone, "checkout", "-q", "-b", "feat-x")
    return bare, clone


def _push_to_remote(root: Path, bare: Path, *, filename: str, content: str, branch: str = "main") -> None:
    push = root / f"push-{branch.replace('/', '-')}-{filename}"
    run_git(root, "clone", "-q", str(bare), str(push))
    run_git(push, "checkout", "-q", "-B", branch)
    (push / filename).write_text(content)
    run_git(push, "add", filename)
    run_git(push, "commit", "-q", "-m", f"advance {filename}")
    run_git(push, "push", "-q", "origin", branch)


def _commit_on_branch(clone: Path, *, filename: str, content: str) -> None:
    (clone / filename).write_text(content)
    run_git(clone, "add", filename)
    run_git(clone, "commit", "-q", "-m", f"branch edit {filename}")


def _currency_task(clone: Path, *, issue: str, extra: dict | None = None) -> Task:
    ticket = Ticket.objects.create(issue_url=issue, extra=extra or {})
    Worktree.objects.create(ticket=ticket, repo_path=str(clone), branch="feat-x", extra={"worktree_path": str(clone)})
    session = Session.objects.create(ticket=ticket)
    return Task.objects.create(ticket=ticket, session=session, phase="testing")


class TestBranchCurrencyBriefLines(TestCase):
    """#2663 dream gap: a testing dispatch must carry a fetched mergeability verdict."""

    def test_conflicting_branch_names_the_path_and_prescribes_merge_not_rebase(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            bare, clone = _remote_and_clone(root)
            _commit_on_branch(clone, filename="a.txt", content="branch side\n")
            _push_to_remote(root, bare, filename="a.txt", content="main side\n")

            brief = "\n".join(branch_currency_brief_lines(_currency_task(clone, issue="https://e.test/i/1")))

            assert "CONFLICTS on: a.txt" in brief
            assert "git merge --no-edit origin/main" in brief
            assert "NEVER rebase" in brief

    def test_behind_but_clean_prescribes_merging_before_gating(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            bare, clone = _remote_and_clone(root)
            _commit_on_branch(clone, filename="a.txt", content="branch side\n")
            _push_to_remote(root, bare, filename="b.txt", content="unrelated\n")

            brief = "\n".join(branch_currency_brief_lines(_currency_task(clone, issue="https://e.test/i/2")))

            assert "1 commit(s) behind origin/main and merges clean." in brief
            assert "CONFLICTS" not in brief

    def test_current_branch_states_the_check_ran(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            _, clone = _remote_and_clone(Path(tmp))

            brief = "\n".join(branch_currency_brief_lines(_currency_task(clone, issue="https://e.test/i/3")))

            assert "feat-x is current with origin/main (fetched at dispatch)." in brief
            assert "UNVERIFIED" not in brief

    def test_no_worktree_is_loud_not_empty(self) -> None:
        ticket = Ticket.objects.create(issue_url="https://e.test/i/4")
        session = Session.objects.create(ticket=ticket)
        task = Task.objects.create(ticket=ticket, session=session, phase="testing")

        lines = branch_currency_brief_lines(task)

        assert lines != ()
        assert "UNVERIFIED — no ticket worktree materialised at dispatch." in "\n".join(lines)

    def test_failed_fetch_is_unverified_never_current(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            bare, clone = _remote_and_clone(Path(tmp))
            shutil.rmtree(bare)

            brief = "\n".join(branch_currency_brief_lines(_currency_task(clone, issue="https://e.test/i/5")))

            assert "UNVERIFIED" in brief
            assert "is current with" not in brief

    def test_ticket_target_override_is_honoured_over_origin_main(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            bare, clone = _remote_and_clone(root)
            _push_to_remote(root, bare, filename="b.txt", content="on the integration branch\n", branch="release/x")

            task = _currency_task(clone, issue="https://e.test/i/6", extra={"target_branch": "release/x"})
            brief = "\n".join(branch_currency_brief_lines(task))

            assert "origin/release/x" in brief
            assert "origin/main" not in brief

    def test_a_non_git_worktree_renders_unverified_without_raising(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            brief = "\n".join(branch_currency_brief_lines(_currency_task(Path(tmp), issue="https://e.test/i/7")))

            assert "UNVERIFIED" in brief
