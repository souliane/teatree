"""Interactive/headless queue building, stale-claim reaping and overlay scoping.

Split verbatim from the former monolithic ``tests/teatree_core/test_selectors.py`` (souliane/teatree#443).
"""

from datetime import timedelta

from django.test import TestCase
from django.utils import timezone

from teatree.core.models import Session, Task, TaskAttempt, Ticket
from teatree.core.selectors import build_automation_summary, build_headless_queue, build_interactive_queue


class TestBuildInteractiveQueue(TestCase):
    def test_returns_non_completed_manual_tasks(self) -> None:
        first_ticket = Ticket.objects.create(state=Ticket.State.STARTED)
        second_ticket = Ticket.objects.create(state=Ticket.State.CODED)
        session = Session.objects.create(ticket=first_ticket, agent_id="codex")
        other_session = Session.objects.create(ticket=second_ticket, agent_id="claude")

        first = Task.objects.create(
            ticket=first_ticket,
            session=session,
            execution_target=Task.ExecutionTarget.INTERACTIVE,
            execution_reason="Need reviewer decision",
        )
        second = Task.objects.create(
            ticket=second_ticket,
            session=other_session,
            execution_target=Task.ExecutionTarget.INTERACTIVE,
            status=Task.Status.CLAIMED,
            claimed_by="codex-terminal",
        )
        Task.objects.create(
            ticket=second_ticket,
            session=other_session,
            execution_target=Task.ExecutionTarget.INTERACTIVE,
            status=Task.Status.COMPLETED,
        )

        queue = build_interactive_queue()
        pending = build_interactive_queue(pending_only=True)

        assert [row.task_id for row in queue] == [first.pk, second.pk]
        assert queue[0].last_error == ""
        assert queue[1].claimed_by == "codex-terminal"
        assert pending == build_interactive_queue(pending_only=True)

    def test_includes_last_error_from_attempts(self) -> None:
        ticket = Ticket.objects.create(state=Ticket.State.STARTED)
        session = Session.objects.create(ticket=ticket, agent_id="codex")
        task = Task.objects.create(
            ticket=ticket,
            session=session,
            execution_target=Task.ExecutionTarget.INTERACTIVE,
            status=Task.Status.CLAIMED,
        )
        TaskAttempt.objects.create(task=task, execution_target="interactive", exit_code=1, error="first error")
        TaskAttempt.objects.create(task=task, execution_target="interactive", exit_code=1, error="ttyd not found")

        queue = build_interactive_queue()

        assert len(queue) == 1
        assert queue[0].last_error == "ttyd not found"

    def test_excludes_failed_tasks(self) -> None:
        ticket = Ticket.objects.create(state=Ticket.State.STARTED)
        session = Session.objects.create(ticket=ticket, agent_id="agent")
        pending = Task.objects.create(
            ticket=ticket,
            session=session,
            execution_target=Task.ExecutionTarget.INTERACTIVE,
        )
        Task.objects.create(
            ticket=ticket,
            session=session,
            execution_target=Task.ExecutionTarget.INTERACTIVE,
            status=Task.Status.FAILED,
        )

        queue = build_interactive_queue()

        assert [row.task_id for row in queue] == [pending.pk]


class TestBuildHeadlessQueue(TestCase):
    def test_excludes_failed_tasks(self) -> None:
        ticket = Ticket.objects.create(state=Ticket.State.STARTED)
        session = Session.objects.create(ticket=ticket, agent_id="agent")
        pending = Task.objects.create(
            ticket=ticket,
            session=session,
            execution_target=Task.ExecutionTarget.HEADLESS,
        )
        Task.objects.create(
            ticket=ticket,
            session=session,
            execution_target=Task.ExecutionTarget.HEADLESS,
            status=Task.Status.FAILED,
        )

        queue = build_headless_queue()

        assert [row.task_id for row in queue] == [pending.pk]

    def test_includes_result_summary(self) -> None:
        ticket = Ticket.objects.create(state=Ticket.State.STARTED)
        session = Session.objects.create(ticket=ticket, agent_id="agent")
        task = Task.objects.create(
            ticket=ticket,
            session=session,
            execution_target=Task.ExecutionTarget.HEADLESS,
            status=Task.Status.CLAIMED,
        )
        TaskAttempt.objects.create(
            task=task,
            execution_target="headless",
            exit_code=0,
            result={"summary": "Fixed 3 files"},
        )

        queue = build_headless_queue()

        assert len(queue) == 1
        assert queue[0].result_summary == "Fixed 3 files"

    def test_includes_session_and_phase(self) -> None:
        ticket = Ticket.objects.create(state=Ticket.State.STARTED)
        session = Session.objects.create(ticket=ticket, agent_id="claude-headless")
        # A free-form phase with no registered agent stays genuinely HEADLESS
        # (a loop-dispatched phase like ``testing`` is routed to INTERACTIVE by
        # the Task.save invariant and would not appear in the headless queue).
        task = Task.objects.create(
            ticket=ticket,
            session=session,
            execution_target=Task.ExecutionTarget.HEADLESS,
            phase="architectural_review",
        )

        queue = build_headless_queue()

        assert len(queue) == 1
        assert queue[0].task_id == task.pk
        assert queue[0].session_agent_id == "claude-headless"
        assert queue[0].phase == "architectural_review"

    def test_includes_ticket_issue_url(self) -> None:
        ticket = Ticket.objects.create(
            state=Ticket.State.STARTED,
            issue_url="https://example.com/issues/555",
        )
        session = Session.objects.create(ticket=ticket, agent_id="agent")
        Task.objects.create(
            ticket=ticket,
            session=session,
            execution_target=Task.ExecutionTarget.HEADLESS,
        )

        queue = build_headless_queue()

        assert len(queue) == 1
        assert queue[0].issue_url == "https://example.com/issues/555"

    def test_include_dismissed(self) -> None:
        """include_dismissed=True should include FAILED tasks but not COMPLETED."""
        ticket = Ticket.objects.create(state=Ticket.State.STARTED)
        session = Session.objects.create(ticket=ticket, agent_id="agent")
        failed = Task.objects.create(
            ticket=ticket,
            session=session,
            execution_target=Task.ExecutionTarget.HEADLESS,
            status=Task.Status.FAILED,
        )
        Task.objects.create(
            ticket=ticket,
            session=session,
            execution_target=Task.ExecutionTarget.HEADLESS,
            status=Task.Status.COMPLETED,
        )
        pending = Task.objects.create(
            ticket=ticket,
            session=session,
            execution_target=Task.ExecutionTarget.HEADLESS,
        )

        queue = build_headless_queue(include_dismissed=True)

        task_ids = [row.task_id for row in queue]
        assert failed.pk in task_ids
        assert pending.pk in task_ids


class TestReapStaleClaims(TestCase):
    def test_reaps_claimed_tasks_with_expired_lease(self) -> None:
        ticket = Ticket.objects.create(state=Ticket.State.STARTED)
        session = Session.objects.create(ticket=ticket, agent_id="agent")
        now = timezone.now()
        stale = Task.objects.create(
            ticket=ticket,
            session=session,
            execution_target=Task.ExecutionTarget.HEADLESS,
            status=Task.Status.CLAIMED,
            claimed_by="dead-worker",
            lease_expires_at=now - timedelta(minutes=5),
        )
        active = Task.objects.create(
            ticket=ticket,
            session=session,
            execution_target=Task.ExecutionTarget.HEADLESS,
            status=Task.Status.CLAIMED,
            claimed_by="live-worker",
            lease_expires_at=now + timedelta(minutes=5),
        )

        reaped = Task.objects.reap_stale_claims()

        assert reaped == 1
        stale.refresh_from_db()
        active.refresh_from_db()
        assert stale.status == Task.Status.FAILED
        assert active.status == Task.Status.CLAIMED

    def test_queue_reaps_before_building(self) -> None:
        ticket = Ticket.objects.create(state=Ticket.State.STARTED)
        session = Session.objects.create(ticket=ticket, agent_id="agent")
        now = timezone.now()
        Task.objects.create(
            ticket=ticket,
            session=session,
            execution_target=Task.ExecutionTarget.HEADLESS,
            status=Task.Status.CLAIMED,
            claimed_by="dead-worker",
            lease_expires_at=now - timedelta(minutes=5),
        )

        queue = build_headless_queue()

        assert len(queue) == 0


class TestHeadlessQueueElapsedTime(TestCase):
    def test_claimed_task_shows_elapsed_and_heartbeat(self) -> None:
        ticket = Ticket.objects.create(state=Ticket.State.STARTED)
        session = Session.objects.create(ticket=ticket, agent_id="agent")
        now = timezone.now()
        Task.objects.create(
            ticket=ticket,
            session=session,
            execution_target=Task.ExecutionTarget.HEADLESS,
            status=Task.Status.CLAIMED,
            claimed_by="worker-1",
            claimed_at=now - timedelta(minutes=5),
            heartbeat_at=now - timedelta(seconds=30),
            lease_expires_at=now + timedelta(minutes=5),
        )

        queue = build_headless_queue()

        assert len(queue) == 1
        assert queue[0].elapsed_time  # non-empty
        assert "5m" in queue[0].elapsed_time
        assert queue[0].heartbeat_age  # non-empty
        assert "30s" in queue[0].heartbeat_age

    def test_pending_task_has_empty_elapsed(self) -> None:
        ticket = Ticket.objects.create(state=Ticket.State.STARTED)
        session = Session.objects.create(ticket=ticket, agent_id="agent")
        Task.objects.create(
            ticket=ticket,
            session=session,
            execution_target=Task.ExecutionTarget.HEADLESS,
        )

        queue = build_headless_queue()

        assert len(queue) == 1
        assert queue[0].elapsed_time == ""
        assert queue[0].heartbeat_age == ""


class TestOverlayFiltering(TestCase):
    """Verify that overlay= parameter filters all selector functions."""

    def test_headless_queue_filters_by_overlay(self) -> None:
        t1 = Ticket.objects.create(overlay="alpha")
        t2 = Ticket.objects.create(overlay="beta")
        s1 = Session.objects.create(ticket=t1, overlay="alpha")
        s2 = Session.objects.create(ticket=t2, overlay="beta")
        Task.objects.create(ticket=t1, session=s1, execution_target="headless")
        Task.objects.create(ticket=t2, session=s2, execution_target="headless")

        assert len(build_headless_queue()) == 2
        assert len(build_headless_queue(overlay="alpha")) == 1

    def test_automation_summary_filters_by_overlay(self) -> None:
        t1 = Ticket.objects.create(overlay="alpha")
        t2 = Ticket.objects.create(overlay="beta")
        s1 = Session.objects.create(ticket=t1, overlay="alpha")
        s2 = Session.objects.create(ticket=t2, overlay="beta")
        task1 = Task.objects.create(ticket=t1, session=s1, execution_target="headless", status=Task.Status.CLAIMED)
        Task.objects.create(ticket=t2, session=s2, execution_target="headless", status=Task.Status.CLAIMED)
        TaskAttempt.objects.create(task=task1, execution_target="headless", exit_code=0, ended_at=timezone.now())

        all_summary = build_automation_summary()
        alpha_summary = build_automation_summary(overlay="alpha")

        assert all_summary.running == 2
        assert alpha_summary.running == 1
        assert alpha_summary.completed_24h == 1
