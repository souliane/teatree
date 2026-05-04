"""Full ticket lifecycle integration test.

Exercises the complete happy-path workflow from ticket creation through delivery,
verifying that state transitions, task auto-scheduling, and quality gates all
chain together correctly.
"""

import subprocess
from pathlib import Path

import pytest
from django.test import TestCase

from teatree.core.models import QualityGateError, Session, Task, Ticket, Worktree


def _make_repo_with_diff(repo_dir: Path, *, branch: str) -> None:
    repo_dir.mkdir(parents=True, exist_ok=True)
    env = {"GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t", "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@t"}

    def git(*args: str) -> None:
        subprocess.run(["git", "-C", str(repo_dir), *args], check=True, env=env, capture_output=True)  # noqa: S607

    git("init", "--initial-branch=main")
    (repo_dir / "README.md").write_text("seed\n")
    git("add", "README.md")
    git("commit", "-m", "seed")
    git("checkout", "-b", branch)
    (repo_dir / "feature.txt").write_text("feature\n")
    git("add", "feature.txt")
    git("commit", "-m", "add feature")


class TestTicketLifecycle(TestCase):
    @pytest.fixture(autouse=True)
    def _inject_tmp_path(self, tmp_path: Path) -> None:
        self._tmp_path = tmp_path

    def test_from_creation_to_tested(self) -> None:
        """Ticket flows from creation through testing with worktree provisioning."""
        ticket = Ticket.objects.create()
        assert ticket.state == "not_started"

        ticket.scope(issue_url="https://gitlab.com/org/repo/-/issues/42", variant="test", repos=["backend"])
        ticket.start()
        ticket.save()
        assert ticket.state == "started"

        wt = Worktree.objects.create(ticket=ticket, repo_path="/tmp/wt/backend", branch="feat/42")
        wt.provision()
        wt.save()
        assert wt.state == "provisioned"
        assert wt.db_name

        ticket.code()
        ticket.test(passed=True)
        ticket.save()
        assert ticket.state == "tested"
        assert ticket.extra.get("tests_passed") is True

        review_task = Task.objects.filter(ticket=ticket, phase="reviewing").first()
        assert review_task is not None
        assert review_task.status == "pending"
        assert review_task.execution_target == "headless"

    def test_from_tested_to_delivered(self) -> None:
        """Ticket flows from tested through delivery via auto-scheduled tasks."""
        ticket = Ticket.objects.create()
        ticket.scope(issue_url="https://gitlab.com/org/repo/-/issues/42", variant="test", repos=["backend"])
        ticket.start()
        ticket.code()
        ticket.test(passed=True)
        ticket.save()

        repo_dir = self._tmp_path / "backend"
        _make_repo_with_diff(repo_dir, branch="feat/42")
        Worktree.objects.create(ticket=ticket, repo_path=str(repo_dir), branch="feat/42")

        session = Session.objects.create(ticket=ticket, agent_id="test-agent")
        session.visit_phase("coding")
        session.visit_phase("testing")
        session.visit_phase("reviewing")
        session.save()

        review_task = Task.objects.get(ticket=ticket, phase="reviewing")
        review_task.claim(claimed_by="headless-agent")
        review_task.save()
        review_task.complete_with_attempt(exit_code=0, result={"summary": "LGTM", "needs_user_input": False})

        ticket.refresh_from_db()
        assert ticket.state == "reviewed"

        ship_task = Task.objects.get(ticket=ticket, phase="shipping")
        ship_task.claim(claimed_by="headless-agent")
        ship_task.save()
        ship_task.complete_with_attempt(exit_code=0, result={"summary": "MR created", "needs_user_input": False})

        ticket.refresh_from_db()
        assert ticket.state == "shipped"

        ticket.request_review()
        ticket.mark_merged()
        ticket.retrospect()
        ticket.mark_delivered()
        ticket.save()
        assert ticket.state == "delivered"
        assert Task.objects.filter(ticket=ticket).count() >= 2


class TestReworkCycle(TestCase):
    def test_resets_progress(self) -> None:
        """Rework from tested -> started clears tests_passed and cancels pending tasks."""
        ticket = Ticket.objects.create()
        ticket.scope(issue_url="https://gitlab.com/org/repo/-/issues/99", variant="", repos=["repo"])
        ticket.start()
        ticket.code()
        ticket.test(passed=True)
        ticket.save()

        assert Task.objects.filter(ticket=ticket, phase="reviewing", status="pending").exists()

        ticket.rework()
        ticket.save()
        assert ticket.state == "started"
        assert not Task.objects.filter(ticket=ticket, status="pending").exists()


class TestQualityGate(TestCase):
    def test_blocks_out_of_order_phases(self) -> None:
        """Session quality gates prevent skipping required phases."""
        ticket = Ticket.objects.create()
        session = Session.objects.create(ticket=ticket, agent_id="agent")

        with pytest.raises(QualityGateError):
            session.check_gate("reviewing")

        session.visit_phase("testing")
        session.check_gate("reviewing")  # should not raise
