"""The hot read paths' query plans, pinned flat in the board size (#4809).

A pin at ONE population is satisfied by a plan that scales — the count simply
happens to be right for that fixture. So each path is measured at two populated
sizes and the SAME count is asserted for both: a count that differs between them
is the failure this file exists to catch, and raising a peg to make it green is
the thing not to do. Each pin also asserts the path actually produced its output,
because a count taken over an empty result proves nothing about the plan.

The loop's two board-walking scanners run on EVERY tick, so an extra query per
ticket there is multiplied by the tick rate and by the whole backlog. The polled
``dash:live`` outcomes read is pinned by its query PLAN instead of its count:
``taskattempt_recent_ended`` is what stops it scanning the whole attempt table
and sorting it into a temp B-tree five seconds apart, and a count cannot see that.

``reconcile_ticket`` is the counter-shape: it answers about ONE ticket, so its cost
must not move with the board at all, and ``reconcile_all`` loops it over every
ticket — an extra whole-table read inside it is N whole-table reads in ``workspace
doctor``. Both are pinned at the same two populations, so neither the per-ticket
cost nor the sweep's per-ticket slope can drift back.
"""

# test-path: cross-cutting — one contract over the loop, standup and dash read paths

import datetime as dt
import io
from collections.abc import Callable
from typing import Any, cast

from django.db import connection
from django.test import TestCase
from django.test.utils import CaptureQueriesContext

from teatree.core.machine_output import call_command_streamed
from teatree.core.models.task_attempt import TaskAttempt
from teatree.core.models.ticket import Ticket
from teatree.core.models.transition import TicketTransition
from teatree.core.worktree.reconcile import reconcile_all, reconcile_ticket, reconcile_work_state_all
from teatree.dash.live import LIVE_OUTCOME_ROWS, OutcomeRow, _outcomes
from teatree.loop.scanners.stale_tickets import StaleTicketsScanner
from tests.factories import (
    PullRequestFactory,
    SessionFactory,
    TaskAttemptFactory,
    TaskFactory,
    TicketFactory,
    WorktreeFactory,
)

#: Each is O(1) in the board size. ``stale_tickets``: the candidate tickets plus
#: one grouped activity aggregate per activity source. ``work_state``: the two
#: workspace roots, every worktree row, the merge-audit evidence and the tickets.
#: ``standup generate``: the windowed transitions, their worktrees, the attempt counts.
STALE_TICKETS_QUERIES = 3
WORK_STATE_QUERIES = 6
STANDUP_QUERIES = 3

#: ``reconcile_ticket`` for one ticket: the two config reads and its own worktree rows.
#: A ticket that HAS a worktree pays one more for the materialised paths the
#: duplicate-scope finder compares against; a done-claiming one pays one more again for
#: the targeted ``EXISTS`` over its own merge audit. Neither read may be issued for a
#: ticket that does not reach it, and neither may be issued unfiltered.
RECONCILE_WORKTREELESS_TICKET_QUERIES = 3
RECONCILE_TICKET_QUERIES = 4
RECONCILE_DONE_CLAIMING_TICKET_QUERIES = 5

#: ``reconcile_all`` = the ticket list, then one ``reconcile_ticket`` per ticket. Its
#: expectation is therefore BUILT from the per-ticket costs above and the seeded board,
#: so a whole-table read creeping into the per-ticket path moves both pins at once.
RECONCILE_ALL_BOARD_QUERIES = 1

_RECONCILE_TICKET_COSTS = (
    (Ticket.State.NOT_STARTED, RECONCILE_WORKTREELESS_TICKET_QUERIES),
    (Ticket.State.STARTED, RECONCILE_TICKET_QUERIES),
    (Ticket.State.MERGED, RECONCILE_DONE_CLAIMING_TICKET_QUERIES),
)

_SMALL, _LARGE = 3, 15
#: Older than ``DEFAULT_STALE_THRESHOLD_DAYS`` so every seeded ticket reads stale,
#: and inside no standup window — the standup's own rows are stamped separately.
_LONG_AGO = dt.timedelta(days=30)


def _populate(scale: int) -> None:
    """A board's worth of rows across the models these paths read."""
    stale_at = dt.datetime.now(dt.UTC) - _LONG_AGO
    for _ in range(scale):
        ticket = TicketFactory(state=Ticket.State.STARTED, overlay="t3-teatree")
        session = SessionFactory(ticket=ticket)
        task = TaskFactory(ticket=ticket, session=session, phase="coding")
        attempt = TaskAttemptFactory(task=task, ended_at=dt.datetime.now(dt.UTC))
        TaskAttempt.objects.filter(pk=attempt.pk).update(started_at=stale_at)
        WorktreeFactory(ticket=ticket)
        PullRequestFactory(ticket=ticket)
        TicketTransition.objects.create(
            ticket=ticket, from_state=Ticket.State.SCOPED, to_state=Ticket.State.STARTED, triggered_by="start"
        )
        WorktreeFactory(ticket=TicketFactory(state=Ticket.State.MERGED, overlay="t3-teatree"))
        # A ticket no worktree was ever cut for: the shape whose reconcile must
        # short-circuit out of the duplicate-scope probe before reading any path.
        TicketFactory(state=Ticket.State.NOT_STARTED, overlay="t3-teatree")


class HotPathQueryPlansTestCase(TestCase):
    """Every board-walking read holds its plan at a small and a large population."""

    def _assert_flat(self, label: str, call: Callable[[], Any], expected: int, *, rows: Callable[[], int]) -> None:
        """Pin *call*'s query count at both populations; *rows* proves it had work to do."""
        for scale in (_SMALL, _LARGE):
            _populate(scale)
            call()  # settles any once-per-process resolution the pin must not measure
            assert rows(), f"{label} had nothing to read at scale {scale} — the pin would be vacuous"
            with self.assertNumQueries(expected, msg=f"{label} at scale {scale}"):
                call()

    def test_the_stale_ticket_scanner_reads_activity_in_bulk_not_per_ticket(self) -> None:
        scanner = StaleTicketsScanner()
        self._assert_flat("stale_tickets", scanner.scan, STALE_TICKETS_QUERIES, rows=lambda: len(scanner.scan()))

    def test_the_work_state_sweep_reads_the_board_once_not_per_ticket(self) -> None:
        # The sweep reports only drift, so the board it walks — not its result — is the work.
        self._assert_flat("work_state", reconcile_work_state_all, WORK_STATE_QUERIES, rows=Ticket.objects.count)

    def test_the_standup_reads_every_reported_tickets_worktrees_in_one_prefetch(self) -> None:
        self._assert_flat("standup generate", _standup, STANDUP_QUERIES, rows=lambda: len(_standup()["yesterday"]))

    def test_reconciling_one_ticket_costs_the_same_whatever_the_board_holds(self) -> None:
        for scale in (_SMALL, _LARGE):
            _populate(scale)
            for state, expected in _RECONCILE_TICKET_COSTS:
                ticket = Ticket.objects.filter(state=state).first()
                assert ticket is not None, f"no {state} ticket seeded — the pin would be vacuous"
                reconcile_ticket(ticket)  # settles any once-per-process resolution
                with self.assertNumQueries(expected, msg=f"reconcile_ticket({state}) at scale {scale}"):
                    reconcile_ticket(ticket)

    def test_the_reconcile_sweep_pays_the_per_ticket_cost_and_nothing_per_board(self) -> None:
        for scale in (_SMALL, _LARGE):
            _populate(scale)
            expected = RECONCILE_ALL_BOARD_QUERIES + sum(
                Ticket.objects.filter(state=state).count() * cost for state, cost in _RECONCILE_TICKET_COSTS
            )
            reconcile_all()  # settles any once-per-process resolution
            with self.assertNumQueries(expected, msg=f"reconcile_all at scale {scale}"):
                reconcile_all()


def _standup() -> dict[str, Any]:
    return cast("dict[str, Any]", call_command_streamed("standup", "generate", stream=io.StringIO()))


class PolledOutcomesIndexTestCase(TestCase):
    """The 5s-polled outcomes read seeks its index instead of sorting the table."""

    def test_the_polled_outcomes_read_neither_scans_the_attempts_nor_sorts_them(self) -> None:
        task = TaskFactory(phase="coding")
        base = dt.datetime(2026, 1, 1, tzinfo=dt.UTC)
        TaskAttempt.objects.bulk_create(
            (TaskAttempt(task=task, ended_at=base + dt.timedelta(seconds=n)) for n in range(200)),
            batch_size=100,
        )

        rows, plan = _outcomes_query_plan()

        assert len(rows) == LIVE_OUTCOME_ROWS, len(rows)
        assert any("taskattempt_recent_ended" in step for step in plan), plan
        assert not any("SCAN teatree_taskattempt" in step for step in plan), plan
        assert not any("TEMP B-TREE" in step for step in plan), plan


def _outcomes_query_plan() -> tuple[tuple[OutcomeRow, ...], list[str]]:
    """``EXPLAIN QUERY PLAN`` for the query ``dash:live``'s own ``_outcomes`` runs.

    Captured from the production call rather than rebuilt from its parts: a
    hand-copied queryset stays green over a production read it has stopped
    describing, which is the one thing a plan pin must never do.
    """
    with CaptureQueriesContext(connection) as captured:
        rows = _outcomes()
    assert len(captured.captured_queries) == 1, captured.captured_queries
    with connection.cursor() as cursor:
        cursor.execute("EXPLAIN QUERY PLAN " + captured.captured_queries[0]["sql"])
        return rows, [row[-1] for row in cursor.fetchall()]
