"""Pinning tests for the single-owner active/terminal Task-state partition.

The partition (PENDING/CLAIMED active; COMPLETED/FAILED terminal) and the
ticket-liveness predicate were copy-pasted across six sites. They now live on
the FSM owners — ``Task.Status.active()`` / ``Task.Status.terminal()`` and
``Ticket.has_active_work()``. ``test_partition_is_total_and_disjoint`` is the
drift guard: a new ``Task.Status`` member that no one classifies fails it.
"""

from datetime import timedelta

from django.test import TestCase
from django.utils import timezone

from teatree.core.models import ConfigSetting, Session, Task, TaskAttempt, Ticket


def _backdate_session(session: Session, *, hours: int) -> None:
    """Move ``started_at`` (``auto_now_add``) back — the only way to age a Session."""
    Session.objects.filter(pk=session.pk).update(started_at=timezone.now() - timedelta(hours=hours))


class TestActiveTerminalPartition(TestCase):
    def test_partition_is_total_and_disjoint(self) -> None:
        active = Task.Status.active()
        terminal = Task.Status.terminal()
        assert active | terminal == set(Task.Status), "every Task.Status member must be classified"
        assert not (active & terminal), "a status cannot be both active and terminal"

    def test_partition_membership(self) -> None:
        assert Task.Status.active() == frozenset({Task.Status.PENDING, Task.Status.CLAIMED})
        assert Task.Status.terminal() == frozenset({Task.Status.COMPLETED, Task.Status.FAILED})


class TestTicketHasActiveWork(TestCase):
    def test_active_task_is_active_work(self) -> None:
        ticket = Ticket.objects.create()
        session = Session.objects.create(ticket=ticket, ended_at=timezone.now())
        Task.objects.create(ticket=ticket, session=session, status=Task.Status.CLAIMED)
        assert ticket.has_active_work() is True

    def test_open_session_is_active_work(self) -> None:
        ticket = Ticket.objects.create()
        Session.objects.create(ticket=ticket)
        assert ticket.has_active_work() is True

    def test_terminal_tasks_with_closed_session_is_not_active_work(self) -> None:
        ticket = Ticket.objects.create()
        session = Session.objects.create(ticket=ticket, ended_at=timezone.now())
        Task.objects.create(ticket=ticket, session=session, status=Task.Status.COMPLETED)
        Task.objects.create(ticket=ticket, session=session, status=Task.Status.FAILED)
        assert ticket.has_active_work() is False


class TestSessionStalenessBound(TestCase):
    """An agent that crashed without closing its Session must not pin the ticket forever.

    The bound is on the SESSION signal only. Every test here that expects
    ``False`` proves the reapers can converge; every test that expects ``True``
    is a fail-CLOSED control — it must go red if the bound is made aggressive
    enough to reap genuinely live work.
    """

    def test_stale_open_session_is_not_active_work(self) -> None:
        ticket = Ticket.objects.create()
        _backdate_session(Session.objects.create(ticket=ticket), hours=48)

        assert ticket.has_active_work() is False

    def test_open_session_inside_the_window_is_active_work(self) -> None:
        ticket = Ticket.objects.create()
        _backdate_session(Session.objects.create(ticket=ticket), hours=2)

        assert ticket.has_active_work() is True

    def test_stale_session_with_a_recent_attempt_is_active_work(self) -> None:
        """A long-running agent whose Session row is old but whose attempt just started."""
        ticket = Ticket.objects.create()
        session = Session.objects.create(ticket=ticket)
        task = Task.objects.create(ticket=ticket, session=session)
        TaskAttempt.objects.create(task=task)
        Task.objects.filter(pk=task.pk).update(status=Task.Status.COMPLETED)
        _backdate_session(session, hours=48)

        assert ticket.has_active_work() is True

    def test_stale_session_with_an_active_task_is_active_work(self) -> None:
        """The task signal carries NO time bound, so the staleness window cannot reap live work."""
        ticket = Ticket.objects.create()
        session = Session.objects.create(ticket=ticket)
        Task.objects.create(ticket=ticket, session=session, status=Task.Status.CLAIMED)
        _backdate_session(session, hours=48)

        assert ticket.has_active_work() is True

    def test_zero_hours_restores_the_unbounded_liveness(self) -> None:
        ConfigSetting.objects.set_value("session_stale_after_hours", 0)
        ticket = Ticket.objects.create()
        _backdate_session(Session.objects.create(ticket=ticket), hours=48)

        assert ticket.has_active_work() is True
