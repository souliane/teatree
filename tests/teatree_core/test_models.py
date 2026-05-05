import subprocess
from pathlib import Path
from unittest.mock import patch

import pytest
from django.contrib import admin
from django.test import TestCase
from django.utils import timezone

from teatree.core import admin as core_admin
from teatree.core.models import (
    InvalidTransitionError,
    MergeRequest,
    QualityGateError,
    Session,
    Task,
    TaskAttempt,
    Ticket,
    Worktree,
)


def _start_with_provision(test_case: TestCase, ticket: Ticket) -> None:
    """Drive ``ticket.start()`` and let the worker schedule the coding task.

    Stage 3 of #140 made provisioning a worker side effect; tests that need
    the coding task materialised swap the worker for an inline stub so the
    side effect runs synchronously without touching real git.
    """
    from unittest.mock import MagicMock  # noqa: PLC0415

    from teatree.core import tasks as tasks_mod  # noqa: PLC0415

    def fake_enqueue(ticket_id: int) -> None:
        target = Ticket.objects.get(pk=ticket_id)
        if target.state == Ticket.State.STARTED:
            target.schedule_coding()

    fake_task = MagicMock()
    fake_task.enqueue.side_effect = fake_enqueue
    with (
        patch.object(tasks_mod, "execute_provision", fake_task),
        test_case.captureOnCommitCallbacks(execute=True),
    ):
        ticket.start()
        ticket.save()


def _advance_ticket_to_tested(ticket: Ticket, test_case: TestCase | None = None) -> None:
    """Advance a ticket through scoped, started, coded, tested.

    When ``test_case`` is provided, the start transition fires its on_commit
    callback so the coding task gets scheduled. Tests that don't care about
    the coding task can omit it.
    """
    ticket.scope(issue_url="https://example.com/issues/123", variant="acme", repos=["backend", "frontend"])
    ticket.save()
    if test_case is not None:
        _start_with_provision(test_case, ticket)
    else:
        ticket.start()
        ticket.save()
    ticket.code()
    ticket.save()
    ticket.test(passed=True)
    ticket.save()


def _complete_phase_task(ticket: Ticket, phase: str) -> None:
    """Find the auto-scheduled task for a phase and complete it."""
    task = ticket.tasks.filter(phase=phase, status=Task.Status.PENDING).first()
    assert task is not None, f"No pending {phase} task found"
    task.claim(claimed_by="test-worker")
    task.complete()


def _attach_shippable_worktree(ticket: Ticket, tmp_path: Path, *, commits_ahead: int = 1) -> Worktree:
    """Attach a git-backed worktree to *ticket* so ``has_shippable_diff`` returns True."""
    repo_dir = tmp_path / f"repo-{ticket.pk}"
    branch = f"feature-{ticket.pk}"
    _init_repo_with_branch(repo_dir, branch=branch, commits_ahead=commits_ahead)
    return Worktree.objects.create(ticket=ticket, repo_path=str(repo_dir), branch=branch)


def _init_repo_with_branch(repo_dir: Path, *, branch: str, commits_ahead: int) -> None:
    """Initialise a git repo at *repo_dir* with main + a feature branch.

    The feature branch is named *branch* and contains *commits_ahead* commits
    on top of the main tip. Use ``commits_ahead=0`` for the no-shippable-diff
    case (branch points at the same SHA as main).
    """
    repo_dir.mkdir(parents=True, exist_ok=True)
    env = {"GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t", "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@t"}

    def git(*args: str) -> None:
        subprocess.run(["git", "-C", str(repo_dir), *args], check=True, env=env, capture_output=True)  # noqa: S607

    git("init", "--initial-branch=main")
    (repo_dir / "README.md").write_text("seed\n")
    git("add", "README.md")
    git("commit", "-m", "seed")
    git("checkout", "-b", branch)
    for i in range(commits_ahead):
        (repo_dir / f"f{i}.txt").write_text(f"{i}\n")
        git("add", f"f{i}.txt")
        git("commit", "-m", f"add f{i}")


class TestTicketNumber(TestCase):
    """``Ticket.ticket_number`` derives a stable identifier from ``issue_url``.

    The fallback to ``str(self.pk)`` covers issue URLs that do not end in a
    valid issue number — empty, non-numeric suffix, or the placeholder ``/0``
    that GitHub and GitLab never assign to a real issue (issue numbers start at 1).
    """

    def test_returns_trailing_number_when_url_is_well_formed(self) -> None:
        ticket = Ticket.objects.create(issue_url="https://github.com/example/repo/issues/123")
        assert ticket.ticket_number == "123"

    def test_falls_back_to_pk_when_url_is_empty(self) -> None:
        ticket = Ticket.objects.create()
        assert ticket.ticket_number == str(ticket.pk)

    def test_falls_back_to_pk_when_url_has_no_trailing_number(self) -> None:
        ticket = Ticket.objects.create(issue_url="https://example.com/no-number")
        assert ticket.ticket_number == str(ticket.pk)

    def test_falls_back_to_pk_when_url_ends_in_zero(self) -> None:
        ticket = Ticket.objects.create(issue_url="https://github.com/example/repo/issues/0")
        assert ticket.ticket_number == str(ticket.pk)


class TestTicketTransitions(TestCase):
    @pytest.fixture(autouse=True)
    def _inject_tmp_path(self, tmp_path: Path) -> None:
        self._tmp_path = tmp_path

    def test_persist_delivery_state(self) -> None:
        ticket = Ticket.objects.create()
        _attach_shippable_worktree(ticket, self._tmp_path)

        _advance_ticket_to_tested(ticket)

        # test() auto-scheduled a reviewing task — complete it to unlock review()
        _complete_phase_task(ticket, "reviewing")
        ticket.refresh_from_db()
        assert ticket.state == Ticket.State.REVIEWED

        # review() auto-scheduled a shipping task — complete it to unlock ship()
        _complete_phase_task(ticket, "shipping")
        ticket.refresh_from_db()
        assert ticket.state == Ticket.State.SHIPPED

        ticket.request_review()
        ticket.save()
        ticket.mark_merged()
        ticket.save()
        ticket.retrospect()
        ticket.save()
        ticket.mark_delivered()
        ticket.save()

        ticket.refresh_from_db()

        assert ticket.state == Ticket.State.DELIVERED
        assert ticket.issue_url == "https://example.com/issues/123"
        assert ticket.variant == "acme"
        assert ticket.repos == ["backend", "frontend"]
        assert ticket.extra["tests_passed"] is True
        assert str(ticket) == "https://example.com/issues/123"

    def test_auto_schedules_review_task(self) -> None:
        ticket = Ticket.objects.create()
        ticket.scope()
        ticket.save()
        ticket.start()
        ticket.save()
        ticket.code()
        ticket.save()
        ticket.test()
        ticket.save()

        # test() auto-schedules a reviewing task
        task = ticket.tasks.get(phase="reviewing")
        assert task.execution_target == Task.ExecutionTarget.HEADLESS
        assert task.session.agent_id == "review"
        assert ticket.state == Ticket.State.TESTED

    def test_review_blocked_without_completed_review_task(self) -> None:
        from django_fsm import TransitionNotAllowed  # noqa: PLC0415

        ticket = Ticket.objects.create()
        _advance_ticket_to_tested(ticket)

        with pytest.raises(TransitionNotAllowed):
            ticket.review()

    def test_reviewing_task_completion_advances_to_reviewed(self) -> None:
        ticket = Ticket.objects.create()
        _attach_shippable_worktree(ticket, self._tmp_path)
        _advance_ticket_to_tested(ticket)

        _complete_phase_task(ticket, "reviewing")
        ticket.refresh_from_db()

        assert ticket.state == Ticket.State.REVIEWED
        # review() also auto-scheduled a shipping task
        assert ticket.tasks.filter(phase="shipping", status=Task.Status.PENDING).exists()

    def test_rework_cancels_pending_tasks(self) -> None:
        ticket = Ticket.objects.create()
        _advance_ticket_to_tested(ticket)

        # There's a pending reviewing task from test()
        assert ticket.tasks.filter(phase="reviewing", status=Task.Status.PENDING).exists()

        ticket.rework()
        ticket.save()

        # Pending tasks should now be failed
        assert not ticket.tasks.filter(status=Task.Status.PENDING).exists()
        assert ticket.tasks.filter(status=Task.Status.FAILED).exists()

    def test_needs_user_input_creates_interactive_followup(self) -> None:
        ticket = Ticket.objects.create()
        _advance_ticket_to_tested(ticket)

        task = ticket.tasks.get(phase="reviewing")
        task.claim(claimed_by="worker")

        # Simulate agent output with needs_user_input
        TaskAttempt.objects.create(
            task=task,
            execution_target=task.execution_target,
            exit_code=0,
            result={"needs_user_input": True, "user_input_reason": "Need design decision"},
        )
        task.complete()

        # Should NOT advance ticket (needs_user_input blocks it)
        ticket.refresh_from_db()
        assert ticket.state == Ticket.State.TESTED

        # Should have created a new interactive task
        interactive = ticket.tasks.filter(
            execution_target=Task.ExecutionTarget.INTERACTIVE,
            status=Task.Status.PENDING,
        )
        assert interactive.count() == 1
        assert interactive.first().execution_reason == "Need design decision"

    def test_rework_returns_to_started_and_clears_testing_fact(self) -> None:
        ticket = Ticket.objects.create()
        ticket.scope()
        ticket.save()
        ticket.start()
        ticket.save()
        ticket.code()
        ticket.save()
        ticket.test(passed=True)
        ticket.save()

        ticket.rework()
        ticket.save()
        ticket.refresh_from_db()

        assert ticket.state == Ticket.State.STARTED
        assert "tests_passed" not in ticket.extra

    def test_ignore_hides_ticket_from_in_flight(self) -> None:
        ticket = Ticket.objects.create()
        ticket.scope()
        ticket.save()
        ticket.start()
        ticket.save()

        assert ticket in Ticket.objects.in_flight()

        ticket.ignore()
        ticket.save()
        ticket.refresh_from_db()

        assert ticket.state == Ticket.State.IGNORED
        assert ticket.extra["ignored_from"] == "started"
        assert ticket not in Ticket.objects.in_flight()

    def test_unignore_restores_previous_state(self) -> None:
        ticket = Ticket.objects.create()
        ticket.scope()
        ticket.save()
        ticket.start()
        ticket.save()
        ticket.code()
        ticket.save()

        ticket.ignore()
        ticket.save()
        assert ticket.state == Ticket.State.IGNORED

        ticket.unignore()
        ticket.save()
        ticket.refresh_from_db()

        assert ticket.state == Ticket.State.CODED
        assert "ignored_from" not in ticket.extra

    def test_rejects_invalid_transition(self) -> None:
        ticket = Ticket.objects.create()

        from django_fsm import TransitionNotAllowed  # noqa: PLC0415

        with pytest.raises(TransitionNotAllowed):
            ticket.review()


class TestHasShippableDiff(TestCase):
    """``Ticket.has_shippable_diff`` and the auto-shipping gate (issue #473)."""

    @pytest.fixture(autouse=True)
    def _inject_tmp_path(self, tmp_path: Path) -> None:
        self._tmp_path = tmp_path

    def _make_ticket_with_worktree(self, *, commits_ahead: int) -> Ticket:
        ticket = Ticket.objects.create()
        repo_dir = self._tmp_path / f"repo-{commits_ahead}"
        branch = "feature"
        _init_repo_with_branch(repo_dir, branch=branch, commits_ahead=commits_ahead)
        Worktree.objects.create(ticket=ticket, repo_path=str(repo_dir), branch=branch)
        return ticket

    def test_returns_false_when_no_worktrees(self) -> None:
        ticket = Ticket.objects.create()

        assert ticket.has_shippable_diff() is False

    def test_returns_false_when_branch_has_no_commits_ahead(self) -> None:
        ticket = self._make_ticket_with_worktree(commits_ahead=0)

        assert ticket.has_shippable_diff() is False

    def test_returns_true_when_branch_has_commits_ahead(self) -> None:
        ticket = self._make_ticket_with_worktree(commits_ahead=2)

        assert ticket.has_shippable_diff() is True

    def test_review_skips_shipping_task_when_no_diff(self) -> None:
        ticket = self._make_ticket_with_worktree(commits_ahead=0)
        _advance_ticket_to_tested(ticket)

        _complete_phase_task(ticket, "reviewing")
        ticket.refresh_from_db()

        assert ticket.state == Ticket.State.REVIEWED
        assert not ticket.tasks.filter(phase="shipping").exists()
        assert "no shippable diff" in ticket.extra.get("shipping_skipped", "")

    def test_review_schedules_shipping_when_branch_has_commits(self) -> None:
        ticket = self._make_ticket_with_worktree(commits_ahead=1)
        _advance_ticket_to_tested(ticket)

        _complete_phase_task(ticket, "reviewing")
        ticket.refresh_from_db()

        assert ticket.state == Ticket.State.REVIEWED
        assert ticket.tasks.filter(phase="shipping", status=Task.Status.PENDING).exists()
        assert "shipping_skipped" not in ticket.extra

    def test_returns_false_when_worktree_missing_branch(self) -> None:
        ticket = Ticket.objects.create()
        Worktree.objects.create(ticket=ticket, repo_path=str(self._tmp_path), branch="")

        assert ticket.has_shippable_diff() is False

    def test_returns_false_when_repo_path_is_not_a_git_directory(self) -> None:
        ticket = Ticket.objects.create()
        not_a_repo = self._tmp_path / "not-a-repo"
        not_a_repo.mkdir()
        Worktree.objects.create(ticket=ticket, repo_path=str(not_a_repo), branch="feature")

        assert ticket.has_shippable_diff() is False


class TestPhaseAutoDispatch(TestCase):
    """Auto-dispatch of next-phase tasks at each phase boundary (issue #364)."""

    @pytest.fixture(autouse=True)
    def _inject_tmp_path(self, tmp_path: Path) -> None:
        self._tmp_path = tmp_path

    def test_start_provisions_then_schedules_coding_task(self) -> None:
        ticket = Ticket.objects.create()
        ticket.scope()
        ticket.save()

        _start_with_provision(self, ticket)

        ticket.refresh_from_db()
        task = ticket.tasks.get(phase="coding")
        assert task.execution_target == Task.ExecutionTarget.HEADLESS
        assert task.session.agent_id == "coding"
        assert ticket.state == Ticket.State.STARTED

    def test_code_auto_schedules_testing_task(self) -> None:
        ticket = Ticket.objects.create()
        ticket.scope()
        ticket.save()
        ticket.start()
        ticket.save()
        ticket.code()
        ticket.save()

        task = ticket.tasks.get(phase="testing")
        assert task.execution_target == Task.ExecutionTarget.HEADLESS
        assert task.session.agent_id == "testing"
        assert ticket.state == Ticket.State.CODED

    def test_scoping_task_completion_advances_to_started(self) -> None:
        from unittest.mock import MagicMock  # noqa: PLC0415

        from teatree.core import tasks as tasks_mod  # noqa: PLC0415

        def fake_enqueue(ticket_id: int) -> None:
            target = Ticket.objects.get(pk=ticket_id)
            if target.state == Ticket.State.STARTED:
                target.schedule_coding()

        ticket = Ticket.objects.create()
        ticket.scope()
        ticket.save()
        session = Session.objects.create(ticket=ticket, agent_id="scoper")
        task = Task.objects.create(ticket=ticket, session=session, phase="scoping")

        fake_task = MagicMock()
        fake_task.enqueue.side_effect = fake_enqueue
        task.claim(claimed_by="worker")
        with (
            patch.object(tasks_mod, "execute_provision", fake_task),
            self.captureOnCommitCallbacks(execute=True),
        ):
            task.complete()

        ticket.refresh_from_db()
        assert ticket.state == Ticket.State.STARTED
        # execute_provision (worker side effect of start()) scheduled the coding task
        assert ticket.tasks.filter(phase="coding", status=Task.Status.PENDING).exists()

    def test_coding_task_completion_advances_to_coded(self) -> None:
        ticket = Ticket.objects.create()
        ticket.scope()
        ticket.save()
        _start_with_provision(self, ticket)

        _complete_phase_task(ticket, "coding")

        ticket.refresh_from_db()
        assert ticket.state == Ticket.State.CODED
        # code() auto-scheduled a testing task
        assert ticket.tasks.filter(phase="testing", status=Task.Status.PENDING).exists()

    def test_testing_task_completion_advances_to_tested(self) -> None:
        ticket = Ticket.objects.create()
        ticket.scope()
        ticket.save()
        _start_with_provision(self, ticket)
        ticket.code()
        ticket.save()

        _complete_phase_task(ticket, "testing")

        ticket.refresh_from_db()
        assert ticket.state == Ticket.State.TESTED
        # test() auto-scheduled a reviewing task
        assert ticket.tasks.filter(phase="reviewing", status=Task.Status.PENDING).exists()

    def test_shipping_defaults_to_interactive_without_t3_auto_ship(self) -> None:
        ticket = Ticket.objects.create()

        with patch.dict("os.environ", {"T3_MODE": "interactive"}, clear=False) as env:
            env.pop("T3_AUTO_SHIP", None)
            task = ticket.schedule_shipping()

        assert task.execution_target == Task.ExecutionTarget.INTERACTIVE
        assert "user approval" in task.execution_reason

    def test_shipping_is_headless_when_t3_auto_ship_true(self) -> None:
        ticket = Ticket.objects.create()

        with patch.dict("os.environ", {"T3_AUTO_SHIP": "true", "T3_MODE": "interactive"}):
            task = ticket.schedule_shipping()

        assert task.execution_target == Task.ExecutionTarget.HEADLESS
        assert "auto mode" in task.execution_reason

    def test_shipping_is_headless_when_global_mode_is_auto(self) -> None:
        # When teatree.mode = auto in ~/.teatree.toml (or T3_MODE=auto), the
        # ship task should run headlessly even without T3_AUTO_SHIP — the
        # global auto mode is the user's blanket consent for publishing.
        ticket = Ticket.objects.create()

        with patch.dict("os.environ", {"T3_MODE": "auto"}, clear=False) as env:
            env.pop("T3_AUTO_SHIP", None)
            task = ticket.schedule_shipping()

        assert task.execution_target == Task.ExecutionTarget.HEADLESS
        assert "auto mode" in task.execution_reason

    def test_shipping_task_completion_advances_to_shipped(self) -> None:
        ticket = Ticket.objects.create()
        _attach_shippable_worktree(ticket, self._tmp_path)
        _advance_ticket_to_tested(ticket)
        _complete_phase_task(ticket, "reviewing")
        # reviewing completion → REVIEWED + shipping task (interactive by default)

        _complete_phase_task(ticket, "shipping")

        ticket.refresh_from_db()
        assert ticket.state == Ticket.State.SHIPPED

    def test_direct_ship_consumes_pending_shipping_task(self) -> None:
        """Regression #471: direct ship() must consume the pending shipping task.

        When ``pr.py`` calls ``ticket.ship()`` directly the auto-scheduled
        PENDING shipping task would otherwise be claimed by the dispatcher
        as a zombie session after the work is already done.
        """
        from unittest.mock import MagicMock  # noqa: PLC0415

        from teatree.core import tasks as tasks_mod  # noqa: PLC0415

        ticket = Ticket.objects.create()
        _attach_shippable_worktree(ticket, self._tmp_path)
        _advance_ticket_to_tested(ticket)
        _complete_phase_task(ticket, "reviewing")
        ticket.refresh_from_db()
        # review() scheduled an interactive shipping task that is still PENDING
        shipping_task = ticket.tasks.get(phase="shipping")
        assert shipping_task.status == Task.Status.PENDING

        with (
            self.captureOnCommitCallbacks(execute=True),
            patch.object(tasks_mod, "execute_ship", MagicMock()),
        ):
            ticket.ship()
            ticket.save()

        shipping_task.refresh_from_db()
        assert shipping_task.status == Task.Status.COMPLETED

    def test_direct_ship_consumes_claimed_shipping_task(self) -> None:
        """Orphan task already CLAIMED by a dispatcher race must still be consumed.

        The dispatcher may have polled and claimed the shipping task between
        the user invoking the manual ship and the FSM transition firing.
        """
        from unittest.mock import MagicMock  # noqa: PLC0415

        from teatree.core import tasks as tasks_mod  # noqa: PLC0415

        ticket = Ticket.objects.create()
        _attach_shippable_worktree(ticket, self._tmp_path)
        _advance_ticket_to_tested(ticket)
        _complete_phase_task(ticket, "reviewing")
        ticket.refresh_from_db()
        shipping_task = ticket.tasks.get(phase="shipping")
        shipping_task.claim(claimed_by="zombie-dispatcher")

        with (
            self.captureOnCommitCallbacks(execute=True),
            patch.object(tasks_mod, "execute_ship", MagicMock()),
        ):
            ticket.ship()
            ticket.save()

        shipping_task.refresh_from_db()
        assert shipping_task.status == Task.Status.COMPLETED

    def test_direct_review_consumes_pending_reviewing_task(self) -> None:
        """Same regression for ``ticket.review()`` and the reviewing task."""
        ticket = Ticket.objects.create()
        _advance_ticket_to_tested(ticket)
        # test() scheduled a PENDING reviewing task. We satisfy the FSM guard
        # by also marking that task COMPLETED before review() — but the bug
        # would surface if a second reviewing task were left pending.
        first_review = ticket.tasks.get(phase="reviewing")
        first_review.claim(claimed_by="t")
        first_review.complete()
        # A second reviewing task could exist (e.g. retry); simulate one.
        ticket.refresh_from_db()
        ticket.state = Ticket.State.TESTED
        ticket.save(update_fields=["state"])
        zombie = ticket.schedule_review()
        assert zombie.status == Task.Status.PENDING

        ticket.review()
        ticket.save()

        zombie.refresh_from_db()
        assert zombie.status == Task.Status.COMPLETED

    def test_task_driven_path_unaffected(self) -> None:
        """Task-completion path stays a single-shipping-task chain.

        ``Task.complete()`` marks the task COMPLETED before
        ``_advance_ticket()`` fires the transition, so the consume call is a
        no-op (zero-row UPDATE). Verify no duplicate task is created.
        """
        ticket = Ticket.objects.create()
        _attach_shippable_worktree(ticket, self._tmp_path)
        _advance_ticket_to_tested(ticket)
        _complete_phase_task(ticket, "reviewing")

        ticket.refresh_from_db()
        assert ticket.state == Ticket.State.REVIEWED
        shipping_tasks = list(ticket.tasks.filter(phase="shipping"))
        assert len(shipping_tasks) == 1
        assert shipping_tasks[0].status == Task.Status.PENDING

    def test_start_enqueues_execute_provision_after_commit(self) -> None:
        """Stage 3 of #140: start() body offloads provisioning to a @task worker."""
        from unittest.mock import MagicMock  # noqa: PLC0415

        from teatree.core import tasks as tasks_mod  # noqa: PLC0415

        ticket = Ticket.objects.create()
        ticket.scope()
        ticket.save()

        fake_task = MagicMock()
        with (
            self.captureOnCommitCallbacks(execute=True),
            patch.object(tasks_mod, "execute_provision", fake_task),
        ):
            ticket.start()
            ticket.save()

        ticket.refresh_from_db()
        assert ticket.state == Ticket.State.STARTED
        fake_task.enqueue.assert_called_once_with(ticket.pk)

    def test_mark_merged_enqueues_execute_teardown_after_commit(self) -> None:
        """Stage 5 of #140: mark_merged() body offloads teardown to a @task worker."""
        from unittest.mock import MagicMock  # noqa: PLC0415

        from teatree.core import tasks as tasks_mod  # noqa: PLC0415

        ticket = Ticket.objects.create()
        ticket.state = Ticket.State.IN_REVIEW
        ticket.save(update_fields=["state"])

        fake_task = MagicMock()
        with (
            self.captureOnCommitCallbacks(execute=True),
            patch.object(tasks_mod, "execute_teardown", fake_task),
        ):
            ticket.mark_merged()
            ticket.save()

        ticket.refresh_from_db()
        assert ticket.state == Ticket.State.MERGED
        fake_task.enqueue.assert_called_once_with(ticket.pk)

    def test_ship_enqueues_execute_ship_after_commit(self) -> None:
        """Stage 2 of #140: ship() body offloads I/O to a @task worker."""
        from unittest.mock import MagicMock  # noqa: PLC0415

        from teatree.core import tasks as tasks_mod  # noqa: PLC0415

        ticket = Ticket.objects.create()
        _advance_ticket_to_tested(ticket)
        _complete_phase_task(ticket, "reviewing")

        ticket.refresh_from_db()
        assert ticket.state == Ticket.State.REVIEWED

        fake_task = MagicMock()
        with (
            self.captureOnCommitCallbacks(execute=True),
            patch.object(tasks_mod, "execute_ship", fake_task),
        ):
            ticket.ship()
            ticket.save()

        ticket.refresh_from_db()
        assert ticket.state == Ticket.State.SHIPPED
        fake_task.enqueue.assert_called_once_with(ticket.pk)

    def test_child_task_of_already_advanced_ticket_is_noop(self) -> None:
        ticket = Ticket.objects.create()
        ticket.scope()
        ticket.save()
        session = Session.objects.create(ticket=ticket, agent_id="scoper")
        first = Task.objects.create(ticket=ticket, session=session, phase="scoping")
        second = Task.objects.create(ticket=ticket, session=session, phase="scoping")

        first.claim(claimed_by="worker-1")
        first.complete()
        # First completion advanced SCOPED → STARTED
        ticket.refresh_from_db()
        assert ticket.state == Ticket.State.STARTED

        second.claim(claimed_by="worker-2")
        second.complete()
        # Second completion no-ops because state is no longer SCOPED
        ticket.refresh_from_db()
        assert ticket.state == Ticket.State.STARTED


class TestWorktree(TestCase):
    def test_lifecycle_transitions_and_stores_urls(self) -> None:
        ticket = Ticket.objects.create(issue_url="https://example.com/issues/42", variant="acme")
        worktree = Worktree.objects.create(ticket=ticket, repo_path="/tmp/backend", branch="teatree-django")

        worktree.provision()
        worktree.save()
        worktree.start_services(services=["backend", "frontend"])
        worktree.save()
        worktree.verify(urls={"backend": "http://localhost:8001", "frontend": "http://localhost:4201"})
        worktree.save()

        worktree.refresh_from_db()

        assert worktree.state == Worktree.State.READY
        assert worktree.db_name == "wt_42_acme"
        assert worktree.extra["services"] == ["backend", "frontend"]
        assert worktree.extra["urls"] == {
            "backend": "http://localhost:8001",
            "frontend": "http://localhost:4201",
        }
        assert str(worktree) == "/tmp/backend"

    def test_full_lifecycle_with_refresh_and_teardown(self) -> None:
        ticket = Ticket.objects.create(issue_url="https://example.com/issues/100")
        worktree = Worktree.objects.create(ticket=ticket, repo_path="/tmp/next", branch="next")

        worktree.provision()
        worktree.save()
        worktree.start_services()
        worktree.save()
        worktree.db_refresh()
        worktree.save()
        worktree.teardown()
        worktree.save()

        worktree.refresh_from_db()

        assert worktree.state == Worktree.State.CREATED
        assert worktree.db_name == ""
        assert worktree.extra == {}

    def test_start_services_allows_restart(self) -> None:
        """Calling start_services when already in SERVICES_UP should work (restart)."""
        ticket = Ticket.objects.create(issue_url="https://example.com/restart", variant="acme")
        worktree = Worktree.objects.create(ticket=ticket, repo_path="/tmp/backend", branch="restart")
        worktree.provision()
        worktree.save()
        worktree.start_services(services=["backend"])
        worktree.save()
        assert worktree.state == Worktree.State.SERVICES_UP

        # Restart — should not raise TransitionNotAllowed
        worktree.start_services(services=["backend", "frontend"])
        worktree.save()
        assert worktree.state == Worktree.State.SERVICES_UP
        assert worktree.extra["services"] == ["backend", "frontend"]

    def test_rejects_invalid_transition(self) -> None:
        worktree = Worktree.objects.create(
            ticket=Ticket.objects.create(),
            repo_path="/tmp/backend",
            branch="broken",
        )

        from django_fsm import TransitionNotAllowed  # noqa: PLC0415

        with pytest.raises(TransitionNotAllowed):
            worktree.verify()


class TestSession(TestCase):
    def test_quality_gates_and_manual_handoff(self) -> None:
        ticket = Ticket.objects.create()
        session = Session.objects.create(ticket=ticket, agent_id="agent-1")

        with pytest.raises(QualityGateError, match="reviewing requires: testing"):
            session.check_gate("reviewing")

        session.visit_phase("testing")
        session.visit_phase("reviewing")
        session.visit_phase("retro")
        session.check_gate("shipping")
        session.begin_manual_handoff()

        session.refresh_from_db()

        assert session.has_visited("testing") is True
        assert session.has_visited("reviewing") is True
        assert session.ended_at is not None
        assert str(session) == "agent-1"

    def test_shipping_gate_blocks_when_retro_not_visited(self) -> None:
        session = Session.objects.create(ticket=Ticket.objects.create())

        session.visit_phase("testing")
        session.visit_phase("reviewing")

        with pytest.raises(QualityGateError, match="shipping requires: retro"):
            session.check_gate("shipping")

    def test_ignores_duplicate_phase_visits_and_force_bypasses_gate(self) -> None:
        session = Session.objects.create(ticket=Ticket.objects.create())

        session.visit_phase("testing")
        session.visit_phase("testing")
        session.check_gate("shipping", force=True)

        assert session.visited_phases == ["testing"]

    def test_visit_phase_records_agent_id(self) -> None:
        session = Session.objects.create(ticket=Ticket.objects.create(), agent_id="agent-1")

        session.visit_phase("coding", agent_id="agent-1")
        session.refresh_from_db()

        assert "coding" in session.phase_visits
        assert session.phase_visits["coding"]["agent_id"] == "agent-1"
        assert "timestamp" in session.phase_visits["coding"]

    def test_visit_phase_without_agent_id_skips_phase_visits(self) -> None:
        session = Session.objects.create(ticket=Ticket.objects.create())

        session.visit_phase("coding")
        session.refresh_from_db()

        assert session.phase_visits == {}
        assert "coding" in session.visited_phases

    def test_maker_checker_rejects_same_agent(self) -> None:
        session = Session.objects.create(ticket=Ticket.objects.create())

        session.visit_phase("testing", agent_id="agent-1")
        session.visit_phase("coding", agent_id="agent-1")
        session.visit_phase("reviewing", agent_id="agent-1")
        session.visit_phase("retro", agent_id="agent-1")

        with pytest.raises(QualityGateError, match="Maker≠checker violation"):
            session.check_gate("shipping")

    def test_maker_checker_allows_different_agents(self) -> None:
        session = Session.objects.create(ticket=Ticket.objects.create())

        session.visit_phase("testing", agent_id="agent-1")
        session.visit_phase("coding", agent_id="agent-1")
        session.visit_phase("reviewing", agent_id="agent-2")
        session.visit_phase("retro", agent_id="agent-1")

        session.check_gate("shipping")  # should not raise

    def test_maker_checker_skipped_without_phase_visits(self) -> None:
        session = Session.objects.create(ticket=Ticket.objects.create())

        session.visit_phase("testing")
        session.visit_phase("coding")
        session.visit_phase("reviewing")
        session.visit_phase("retro")

        session.check_gate("shipping")  # no agent_ids recorded → no enforcement

    def test_maker_checker_bypassed_with_force(self) -> None:
        session = Session.objects.create(ticket=Ticket.objects.create())

        session.visit_phase("testing", agent_id="agent-1")
        session.visit_phase("coding", agent_id="agent-1")
        session.visit_phase("reviewing", agent_id="agent-1")

        session.check_gate("shipping", force=True)  # force bypasses all checks

    def test_repo_tracking(self) -> None:
        session = Session.objects.create(ticket=Ticket.objects.create())

        session.mark_repo_modified("backend")
        session.mark_repo_modified("frontend")
        session.mark_repo_modified("backend")  # duplicate
        session.mark_repo_tested("backend")

        session.refresh_from_db()
        assert session.repos_modified == ["backend", "frontend"]
        assert session.repos_tested == ["backend"]
        assert session.untested_repos() == ["frontend"]


class TestTask(TestCase):
    def test_claim_route_complete_fail_and_attempt_storage(self) -> None:
        ticket = Ticket.objects.create()
        session = Session.objects.create(ticket=ticket, agent_id="agent-1")
        task = Task.objects.create(ticket=ticket, session=session)

        task.claim(claimed_by="worker-1", lease_seconds=120)
        first_expiry = task.lease_expires_at
        assert first_expiry is not None

        task.renew_lease(lease_seconds=300)
        task.route_to_interactive(reason="needs manual follow-up")
        task.complete(result_artifact_path="/tmp/result.json")

        failed_task = Task.objects.create(ticket=ticket, session=session)
        failed_task.fail()

        attempt = TaskAttempt.objects.create(
            task=task,
            execution_target=Task.ExecutionTarget.INTERACTIVE,
            ended_at=timezone.now(),
            exit_code=0,
            artifact_path="/tmp/result.json",
        )

        task.refresh_from_db()
        failed_task.refresh_from_db()

        assert task.status == Task.Status.COMPLETED
        assert task.execution_target == Task.ExecutionTarget.INTERACTIVE
        assert task.execution_reason == "needs manual follow-up"
        assert task.result_artifact_path == "/tmp/result.json"
        assert task.claimed_by == ""
        assert task.lease_expires_at is None
        assert failed_task.status == Task.Status.FAILED
        assert attempt.execution_target == Task.ExecutionTarget.INTERACTIVE
        assert str(task) == f"task-{task.pk}-{Task.ExecutionTarget.INTERACTIVE}"
        assert str(attempt) == f"attempt-{attempt.pk}"

    def test_claim_rejects_active_lease_and_sdk_routing_resets_claim(self) -> None:
        ticket = Ticket.objects.create()
        session = Session.objects.create(ticket=ticket)
        task = Task.objects.create(ticket=ticket, session=session)

        task.claim(claimed_by="worker-1")

        with pytest.raises(InvalidTransitionError, match="Task already claimed"):
            task.claim(claimed_by="worker-2")

        task.route_to_headless(reason="retry in sdk")
        task.refresh_from_db()

        assert task.execution_target == Task.ExecutionTarget.HEADLESS
        assert task.execution_reason == "retry in sdk"
        assert task.status == Task.Status.PENDING
        assert task.claimed_by == ""

    def test_claim_rejects_terminal_tasks(self) -> None:
        ticket = Ticket.objects.create()
        session = Session.objects.create(ticket=ticket)
        completed = Task.objects.create(ticket=ticket, session=session, status=Task.Status.COMPLETED)
        failed = Task.objects.create(ticket=ticket, session=session, status=Task.Status.FAILED)

        with pytest.raises(InvalidTransitionError, match="Task already finished"):
            completed.claim(claimed_by="worker-1")

        with pytest.raises(InvalidTransitionError, match="Task already finished"):
            failed.claim(claimed_by="worker-2")

    def test_complete_with_attempt_records_success_and_failure(self) -> None:
        ticket = Ticket.objects.create()
        session = Session.objects.create(ticket=ticket, agent_id="agent-1")

        success_task = Task.objects.create(ticket=ticket, session=session)
        attempt = success_task.complete_with_attempt(artifact_path="/tmp/ok.json")
        success_task.refresh_from_db()
        assert success_task.status == Task.Status.COMPLETED
        assert attempt.exit_code == 0
        assert attempt.artifact_path == "/tmp/ok.json"

        failure_task = Task.objects.create(ticket=ticket, session=session)
        attempt = failure_task.complete_with_attempt(exit_code=1, error="boom")
        failure_task.refresh_from_db()
        assert failure_task.status == Task.Status.FAILED
        assert attempt.exit_code == 1
        assert attempt.error == "boom"

    def test_parent_task_linkage_in_interactive_followup(self) -> None:
        ticket = Ticket.objects.create()
        _advance_ticket_to_tested(ticket)

        parent = ticket.tasks.get(phase="reviewing")
        parent.claim(claimed_by="worker")

        TaskAttempt.objects.create(
            task=parent,
            execution_target=parent.execution_target,
            exit_code=0,
            result={"needs_user_input": True, "user_input_reason": "Need input"},
        )
        parent.complete()

        child = ticket.tasks.filter(
            execution_target=Task.ExecutionTarget.INTERACTIVE,
            status=Task.Status.PENDING,
        ).first()
        assert child is not None
        assert child.parent_task_id == parent.pk
        assert list(parent.child_tasks.values_list("pk", flat=True)) == [child.pk]

    def test_reopen_failed_task_resets_to_pending(self) -> None:
        ticket = Ticket.objects.create()
        session = Session.objects.create(ticket=ticket)
        task = Task.objects.create(ticket=ticket, session=session, status=Task.Status.FAILED)

        task.reopen()
        task.refresh_from_db()

        assert task.status == Task.Status.PENDING

    def test_reopen_non_failed_task_raises(self) -> None:
        ticket = Ticket.objects.create()
        session = Session.objects.create(ticket=ticket)
        task = Task.objects.create(ticket=ticket, session=session, status=Task.Status.PENDING)

        with pytest.raises(InvalidTransitionError, match="Can only reopen failed tasks"):
            task.reopen()


class TestChildTaskSpawning(TestCase):
    def test_spawn_child_tasks_creates_per_repo_tasks(self) -> None:
        ticket = Ticket.objects.create()
        session = Session.objects.create(ticket=ticket, agent_id="worker")
        parent = Task.objects.create(ticket=ticket, session=session, phase="coding")

        children = parent.spawn_child_tasks(["backend", "frontend", "translations"])

        assert len(children) == 3
        assert all(c.parent_task_id == parent.pk for c in children)
        assert all(c.phase == "coding" for c in children)
        assert [c.execution_reason for c in children] == [
            "Repo: backend",
            "Repo: frontend",
            "Repo: translations",
        ]

    def test_all_children_done(self) -> None:
        ticket = Ticket.objects.create()
        session = Session.objects.create(ticket=ticket)
        parent = Task.objects.create(ticket=ticket, session=session)
        children = parent.spawn_child_tasks(["a", "b"])

        assert not parent.all_children_done()

        children[0].status = Task.Status.COMPLETED
        children[0].save(update_fields=["status"])
        assert not parent.all_children_done()

        children[1].status = Task.Status.FAILED
        children[1].save(update_fields=["status"])
        assert parent.all_children_done()


class TestBuildTaskDetail(TestCase):
    def test_returns_full_lineage(self) -> None:
        from teatree.core.selectors import build_task_detail  # noqa: PLC0415

        ticket = Ticket.objects.create()
        session = Session.objects.create(ticket=ticket, agent_id="test")
        parent = Task.objects.create(ticket=ticket, session=session, phase="coding")
        child = Task.objects.create(ticket=ticket, session=session, phase="reviewing", parent_task=parent)
        TaskAttempt.objects.create(
            task=parent,
            execution_target=Task.ExecutionTarget.HEADLESS,
            exit_code=0,
            result={"summary": "done", "files_modified": ["/a.py"]},
        )

        detail = build_task_detail(parent.pk)
        assert detail is not None
        assert detail.task_id == parent.pk
        assert detail.parent is None
        assert len(detail.children) == 1
        assert detail.children[0].task_id == child.pk
        assert len(detail.attempts) == 1
        assert detail.attempts[0].result == {"summary": "done", "files_modified": ["/a.py"]}

        child_detail = build_task_detail(child.pk)
        assert child_detail is not None
        assert child_detail.parent is not None
        assert child_detail.parent.task_id == parent.pk
        assert child_detail.children == []

    def test_returns_none_for_missing(self) -> None:
        from teatree.core.selectors import build_task_detail  # noqa: PLC0415

        assert build_task_detail(999999) is None


class TestAdmin(TestCase):
    def test_registers_all_core_models(self) -> None:
        registry = admin.site._registry

        assert Ticket in registry
        assert Worktree in registry
        assert Session in registry
        assert Task in registry
        assert TaskAttempt in registry
        assert core_admin is not None


class TestMergeRequestModel(TestCase):
    def test_str_representation(self) -> None:
        ticket = Ticket.objects.create(overlay="test")
        mr = MergeRequest.objects.create(
            ticket=ticket,
            url="https://example.com/repo/-/merge_requests/1",
            repo="my-repo",
            iid="42",
        )
        assert str(mr) == "my-repo #42"

    def test_request_review_transition(self) -> None:
        ticket = Ticket.objects.create(overlay="test")
        mr = MergeRequest.objects.create(
            ticket=ticket,
            url="https://example.com/repo/-/merge_requests/2",
            repo="my-repo",
            iid="43",
        )
        mr.request_review(slack_url="https://slack.com/msg/123")
        mr.save()
        mr.refresh_from_db()
        assert mr.state == MergeRequest.State.REVIEW_REQUESTED
        assert mr.slack_url == "https://slack.com/msg/123"
        assert mr.review_requested_at is not None

    def test_approve_transition(self) -> None:
        ticket = Ticket.objects.create(overlay="test")
        mr = MergeRequest.objects.create(
            ticket=ticket,
            url="https://example.com/repo/-/merge_requests/3",
            repo="my-repo",
            iid="44",
            state=MergeRequest.State.REVIEW_REQUESTED,
        )
        mr.approve()
        mr.save()
        assert mr.state == MergeRequest.State.APPROVED

    def test_mark_merged_transition(self) -> None:
        ticket = Ticket.objects.create(overlay="test")
        mr = MergeRequest.objects.create(
            ticket=ticket,
            url="https://example.com/repo/-/merge_requests/4",
            repo="my-repo",
            iid="45",
        )
        mr.mark_merged()
        mr.save()
        assert mr.state == MergeRequest.State.MERGED
