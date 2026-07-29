"""Every dashboard page's query plan, pinned exactly and proven flat (#3873).

A pin at ONE population size is satisfied by a plan that scales — the count simply
happens to be right for that fixture. So each page is measured twice, at two
populated sizes, and both the exact count and its invariance are asserted. The board
and the health bands matter most: both are polled, so an N+1 there is multiplied by
the poll rate rather than paid once.

``assertNumQueries`` is the whole point of the file — a page that grows a query is a
failure here, and raising a peg to make this green is the thing not to do.
"""

# test-path: cross-cutting — one contract over every dash page, seeded across the core models they read

from uuid import uuid4

from django.core.cache import cache
from django.test import TestCase
from django.urls import reverse

from teatree.core.models import Loop, Mode
from teatree.core.models.pull_request import PullRequest
from teatree.core.models.session import Session
from teatree.core.models.task_attempt import TaskAttempt
from teatree.core.models.ticket import Ticket
from teatree.core.models.transition import TicketTransition
from tests.factories import TaskFactory, TicketFactory

State = Ticket.State

_LOOPBACK = {"REMOTE_ADDR": "127.0.0.1"}

#: url name -> the exact number of queries the page issues, at any population size.
PAGE_QUERY_PINS: dict[str, int] = {
    "dash:board": 11,
    "dash:board_columns": 9,
    "dash:health": 20,
    "dash:health_bands": 20,
    "dash:loops": 17,
    "dash:loops_table": 17,
    "dash:presets": 15,
    "dash:sessions": 3,
    "dash:settings": 5,
    "dash:settings_readouts": 3,
}
TICKET_DRAWER_QUERIES = 11
TRANSCRIPT_QUERIES = 2


def _populate(scale: int) -> Ticket:
    """A dashboard's worth of rows across every model the pages read."""
    ticket = Ticket.objects.create(state=State.STARTED)
    for index in range(scale):
        each = TicketFactory(state=State.STARTED)
        task = TaskFactory(ticket=each, phase="coding")
        TaskAttempt.objects.create(
            task=task,
            execution_target="headless",
            model="claude-opus-4-8",
            agent_session_id=f"sess-{each.pk}",
        )
        Session.objects.create(ticket=each, overlay="t3-teatree")
        PullRequest.objects.create(ticket=each, url=f"https://example.test/{each.pk}", repo="r", iid=str(each.pk))
        TicketTransition.objects.create(
            ticket=each, from_state=State.SCOPED, to_state=State.STARTED, triggered_by="start"
        )
        unique = f"{index}-{scale}-{uuid4().hex[:8]}"
        Loop.objects.create(name=f"loop-{unique}", delay_seconds=60, script="run.py")
        Mode.objects.create(name=f"preset-{unique}", entries={})
    return ticket


class DashboardPageQueryPlansTestCase(TestCase):
    """Each page's plan, asserted at a small and a large population."""

    def setUp(self) -> None:
        # The health page memoizes its spend chip for 30s, so a warm cache would make
        # the second measurement cheaper than the first and hide a plan that scales.
        # Every measurement here is the COLD plan — the one an operator pays.
        cache.clear()
        self.addCleanup(cache.clear)

    def _assert_pinned(self, url: str, expected: int) -> None:
        for scale in (3, 15):
            _populate(scale)
            cache.clear()
            with self.assertNumQueries(expected, msg=f"{url} at scale {scale}"):
                assert self.client.get(url, **_LOOPBACK).status_code == 200

    def test_every_page_holds_its_pinned_query_count_at_two_populations(self) -> None:
        for name, expected in PAGE_QUERY_PINS.items():
            with self.subTest(page=name):
                self._assert_pinned(reverse(name), expected)

    def test_the_ticket_drawer_holds_its_plan_however_much_history_the_ticket_has(self) -> None:
        ticket = _populate(3)
        for scale in (4, 40):
            task = TaskFactory(ticket=ticket, phase="coding")
            TaskAttempt.objects.bulk_create(TaskAttempt(task=task, execution_target="headless") for _ in range(scale))
            TicketTransition.objects.bulk_create(
                TicketTransition(ticket=ticket, from_state=State.SCOPED, to_state=State.STARTED, triggered_by="start")
                for _ in range(scale)
            )
            url = reverse("dash:ticket_drawer", args=[ticket.pk])
            with self.assertNumQueries(TICKET_DRAWER_QUERIES, msg=f"drawer at scale {scale}"):
                assert self.client.get(url, **_LOOPBACK).status_code == 200

    def test_the_transcript_page_reads_the_filesystem_not_the_database(self) -> None:
        _populate(3)
        url = reverse("dash:transcript", args=["sess-missing"])
        with self.assertNumQueries(TRANSCRIPT_QUERIES):
            assert self.client.get(url, **_LOOPBACK).status_code == 200
