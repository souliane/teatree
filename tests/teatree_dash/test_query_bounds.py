"""Each dashboard panel's query count must NOT scale with row count (#3674).

The acceptance bar: a panel's query count is bounded by a fixed upper limit
independent of how many tickets / tasks / attempts it renders. Each test compares
two POPULATED sizes (a degenerate empty set short-circuits ``pk__in=[]`` queries
and would understate the count) and asserts the plan is flat, plus pins a fixed
upper bound so a future N+1 regression turns it red.
"""

from django.core.cache import cache
from django.db import connection
from django.test import TestCase
from django.test.utils import CaptureQueriesContext

from teatree.core.models.ticket import Ticket
from teatree.dash.health_bands import build_health_view
from teatree.dash.selectors import build_kanban_columns
from tests.factories import TaskAttemptFactory, TaskFactory, TicketFactory

State = Ticket.State

_BOARD_MAX_QUERIES = 12
_HEALTH_MAX_QUERIES = 30


def _seed_board(n: int) -> None:
    for _ in range(n):
        ticket = TicketFactory(state=State.STARTED)
        TaskAttemptFactory(task=TaskFactory(ticket=ticket))


class BoardQueryBoundTestCase(TestCase):
    def test_board_query_count_is_flat_across_ticket_count(self) -> None:
        _seed_board(5)
        with CaptureQueriesContext(connection) as few:
            build_kanban_columns()
        _seed_board(45)
        with CaptureQueriesContext(connection) as many:
            build_kanban_columns()

        assert len(few) == len(many), f"board query plan scales with rows: {len(few)} -> {len(many)}"
        assert len(many) <= _BOARD_MAX_QUERIES, f"board over the {_BOARD_MAX_QUERIES}-query bound: {len(many)}"


class HealthQueryBoundTestCase(TestCase):
    def setUp(self) -> None:
        cache.clear()
        self.addCleanup(cache.clear)

    def _seed_attempts(self, n: int, ticket: Ticket) -> None:
        for _ in range(n):
            TaskAttemptFactory(task=TaskFactory(ticket=ticket), model="claude")

    def test_health_view_query_count_is_flat_across_attempt_count(self) -> None:
        ticket = TicketFactory(state=State.STARTED)
        self._seed_attempts(20, ticket)
        cache.clear()
        with CaptureQueriesContext(connection) as few:
            build_health_view()

        self._seed_attempts(40, ticket)
        cache.clear()
        with CaptureQueriesContext(connection) as many:
            build_health_view()

        assert len(few) == len(many), f"health query plan scales with rows: {len(few)} -> {len(many)}"
        assert len(many) <= _HEALTH_MAX_QUERIES, f"health over the {_HEALTH_MAX_QUERIES}-query bound: {len(many)}"
