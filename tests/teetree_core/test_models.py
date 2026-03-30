import pytest
from django.contrib import admin
from django.utils import timezone

from teetree.core import admin as core_admin
from teetree.core.models import (
    InvalidTransitionError,
    QualityGateError,
    Session,
    Task,
    TaskAttempt,
    Ticket,
    Worktree,
)


def _advance_ticket_to_tested(ticket: Ticket) -> None:
    """Advance a ticket through scoped, started, coded, tested."""
    ticket.scope(issue_url="https://example.com/issues/123", variant="acme", repos=["backend", "frontend"])
    ticket.save()
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


@pytest.mark.django_db
class TestTicketTransitions:
    def test_persist_delivery_state(self) -> None:
        ticket = Ticket.objects.create()

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

    def test_rejects_invalid_transition(self) -> None:
        ticket = Ticket.objects.create()

        from django_fsm import TransitionNotAllowed  # noqa: PLC0415

        with pytest.raises(TransitionNotAllowed):
            ticket.review()


@pytest.mark.django_db
class TestWorktree:
    def test_lifecycle_allocates_ports_and_urls(self) -> None:
        ticket = Ticket.objects.create(issue_url="https://example.com/issues/42", variant="acme")
        worktree = Worktree.objects.create(ticket=ticket, repo_path="/tmp/backend", branch="teatree-django")

        worktree.provision()
        worktree.save()
        worktree.start_services(services=["backend", "frontend"])
        worktree.save()
        worktree.verify()
        worktree.save()

        worktree.refresh_from_db()

        assert worktree.state == Worktree.State.READY
        assert worktree.ports == {
            "backend": 8001,
            "frontend": 4201,
            "postgres": 5433,
            "redis": 6379,
        }
        assert worktree.db_name == "wt_42_acme"
        assert worktree.extra["services"] == ["backend", "frontend"]
        assert worktree.extra["urls"] == {
            "backend": "http://localhost:8001",
            "frontend": "http://localhost:4201",
        }
        assert str(worktree) == "/tmp/backend"

    def test_reuses_next_available_ports_and_allows_refresh_teardown(self) -> None:
        ticket = Ticket.objects.create(issue_url="https://example.com/issues/100")
        occupied = Worktree.objects.create(
            ticket=ticket,
            repo_path="/tmp/occupied",
            branch="occupied",
            state=Worktree.State.PROVISIONED,
            ports={"backend": 8001, "frontend": 4201, "postgres": 5433, "redis": 6379},
        )
        Worktree.objects.create(
            ticket=ticket,
            repo_path="/tmp/weird",
            branch="weird",
            state=Worktree.State.PROVISIONED,
            ports=["not", "a", "dict"],
        )
        Worktree.objects.create(
            ticket=ticket,
            repo_path="/tmp/backend-only",
            branch="backend-only",
            state=Worktree.State.PROVISIONED,
            ports={"backend": 8010},
        )
        Worktree.objects.create(
            ticket=ticket,
            repo_path="/tmp/frontend-only",
            branch="frontend-only",
            state=Worktree.State.PROVISIONED,
            ports={"frontend": 4210},
        )
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
        occupied.refresh_from_db()

        assert occupied.ports["backend"] == 8001
        assert worktree.state == Worktree.State.CREATED
        assert worktree.ports == {}
        assert worktree.db_name == ""
        assert worktree.extra == {}

    def test_rejects_invalid_transition(self) -> None:
        worktree = Worktree.objects.create(
            ticket=Ticket.objects.create(),
            repo_path="/tmp/backend",
            branch="broken",
        )

        from django_fsm import TransitionNotAllowed  # noqa: PLC0415

        with pytest.raises(TransitionNotAllowed):
            worktree.verify()

    def test_port_available_returns_false_on_os_error(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Worktree._port_available returns False when binding raises OSError."""
        import socket  # noqa: PLC0415

        def _bind_raises(self: socket.socket, address: tuple[str, int]) -> None:
            msg = "Address already in use"
            raise OSError(msg)

        monkeypatch.setattr(socket.socket, "bind", _bind_raises)

        assert Worktree._port_available(8001) is False

    def test_port_available_returns_true_on_success(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Worktree._port_available returns True when binding succeeds."""
        import socket  # noqa: PLC0415

        monkeypatch.setattr(socket.socket, "bind", lambda self, addr: None)

        assert Worktree._port_available(8001) is True

    def test_refresh_ports_fills_missing_key(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """When ports are incomplete but allocating produces the same result, no save happens."""
        ticket = Ticket.objects.create(issue_url="https://example.com/issues/55")
        worktree = Worktree.objects.create(
            ticket=ticket,
            repo_path="/tmp/backend",
            branch="teatree-django",
            state=Worktree.State.PROVISIONED,
            # Missing "postgres" key — incomplete, so falls through to allocate
            ports={"backend": 8001, "frontend": 4201, "redis": 6379},
        )

        monkeypatch.setattr(Worktree, "_port_available", staticmethod(lambda _port: True))
        monkeypatch.setattr(
            Worktree,
            "_allocate_ports",
            # Allocate returns ports that, after merge with current, equal current
            lambda self: {"backend": 9999, "frontend": 9998, "postgres": 5433, "redis": 6380},
        )
        # Merge fills in missing postgres from _allocate_ports, triggers save
        assert worktree.refresh_ports_if_needed() is True
        worktree.refresh_from_db()
        assert worktree.ports["postgres"] == 5433

    def test_refresh_ports_noop_when_all_keys_present(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """When all required keys are present, refresh does nothing."""
        ticket = Ticket.objects.create(issue_url="https://example.com/issues/56")
        worktree = Worktree.objects.create(
            ticket=ticket,
            repo_path="/tmp/backend",
            branch="teatree-django",
            state=Worktree.State.PROVISIONED,
            ports={"backend": 8001, "frontend": 4201, "postgres": 5433, "redis": 6379},
        )

        assert worktree.refresh_ports_if_needed() is False

    def test_refresh_ports_noop_when_allocate_matches_existing(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """When incomplete ports + allocate produces same merged result, no DB write."""
        ticket = Ticket.objects.create(issue_url="https://example.com/issues/57")
        # Missing "postgres" key triggers allocation
        current_ports = {"backend": 8001, "frontend": 4201, "redis": 6379}
        worktree = Worktree.objects.create(
            ticket=ticket,
            repo_path="/tmp/backend",
            branch="teatree-django",
            state=Worktree.State.PROVISIONED,
            ports=dict(current_ports),
        )

        # Allocate returns same keys+values as current — merged == current
        monkeypatch.setattr(
            Worktree,
            "_allocate_ports",
            lambda self: dict(current_ports),
        )
        result = worktree.refresh_ports_if_needed()
        assert result is False


@pytest.mark.django_db
class TestSession:
    def test_quality_gates_and_manual_handoff(self) -> None:
        ticket = Ticket.objects.create()
        session = Session.objects.create(ticket=ticket, agent_id="agent-1")

        with pytest.raises(QualityGateError, match="reviewing requires: testing"):
            session.check_gate("reviewing")

        session.visit_phase("testing")
        session.visit_phase("reviewing")
        session.check_gate("shipping")
        session.begin_manual_handoff()

        session.refresh_from_db()

        assert session.has_visited("testing") is True
        assert session.has_visited("reviewing") is True
        assert session.ended_at is not None
        assert str(session) == "agent-1"

    def test_ignores_duplicate_phase_visits_and_force_bypasses_gate(self) -> None:
        session = Session.objects.create(ticket=Ticket.objects.create())

        session.visit_phase("testing")
        session.visit_phase("testing")
        session.check_gate("shipping", force=True)

        assert session.visited_phases == ["testing"]


@pytest.mark.django_db
class TestTask:
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


@pytest.mark.django_db
class TestBuildTaskDetail:
    def test_returns_full_lineage(self) -> None:
        from teetree.core.selectors import build_task_detail  # noqa: PLC0415

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
        from teetree.core.selectors import build_task_detail  # noqa: PLC0415

        assert build_task_detail(999999) is None


@pytest.mark.django_db
class TestAdmin:
    def test_registers_all_core_models(self) -> None:
        registry = admin.site._registry

        assert Ticket in registry
        assert Worktree in registry
        assert Session in registry
        assert Task in registry
        assert TaskAttempt in registry
        assert core_admin is not None
