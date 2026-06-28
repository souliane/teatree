"""Pinning tests for the single-owner active/terminal Task-state partition.

The partition (PENDING/CLAIMED active; COMPLETED/FAILED terminal) and the
ticket-liveness predicate were copy-pasted across six sites. They now live on
the FSM owners — ``Task.Status.active()`` / ``Task.Status.terminal()`` and
``Ticket.has_active_work()``. ``test_partition_is_total_and_disjoint`` is the
drift guard: a new ``Task.Status`` member that no one classifies fails it.
"""

from django.test import TestCase
from django.utils import timezone

from teatree.core.models import Session, Task, Ticket


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
