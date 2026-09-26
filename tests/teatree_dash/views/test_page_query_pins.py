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

import pytest
from django.core.cache import cache
from django.test import TestCase
from django.urls import reverse

from teatree.core.models import Loop, Mode, Task
from teatree.core.models.pull_request import PullRequest
from teatree.core.models.session import Session
from teatree.core.models.task_attempt import TaskAttempt
from teatree.core.models.ticket import Ticket
from teatree.core.models.transition import TicketTransition
from tests.factories import TicketFactory
from tests.teatree_dash._overlay_venue import core_overlay_only

State = Ticket.State

_LOOPBACK = {"REMOTE_ADDR": "127.0.0.1"}

#: url name -> the exact number of queries the page issues, at any population size.
#:
#: The pages that read the loop fleet each carry the #4185 starvation axis: ONE bounded
#: ``status__in`` read of the live ``loop_timer`` rows, plus the effective-verdict bulk
#: read where the page did not already resolve it. Both are O(1) in the population — the
#: two-population assertion below is what proves that, and it is the invariant this file
#: exists to hold. A count that differs BETWEEN the two populations is the failure; a
#: flat count that rose because the page reads a new datum is a re-pin, not an N+1.
#:
#: #4196 folded the effective verdict onto the resolver the tick itself gates on, and
#: routed the loop-list/statusline read model through it too. That nets OUT on every page
#: that already resolved the operating mode — health, the bands and the loop table each
#: shed the duplicate preset read they used to pay. ``live`` resolved no mode at all, so
#: it now pays the L0 default lookup its membership read needs. Still flat, still one
#: bounded read per datum.
#: #4202 collapsed the mode to a pure loop table. The loops header offers the LIVE set of mode
#: names instead of three hard-coded posture tokens, which is one bounded ``Mode`` name read,
#: O(1) in the population.
#: The tri-state manual layer nets the presence-upgrade read AWAY — nothing reads
#: ``presence_upgrade_mode`` any more — and replaces it with the manual-override map plus the
#: override-reason read the deciding-layer column renders: +1 on the two health pages, +2 on
#: the loop pages, each ONE bounded read of the whole fleet rather than one per row. ``presets``
#: pays one FEWER: totality removed the base-tier lookup a preset with no opinion used to need.
#: Every one of these is flat across both populations — which is the only thing that makes a
#: re-pin honest here, and it was measured before the numbers were touched.
#: #4340's transfer page reads no work at all — its scope statement is code — so its two
#: queries are the nav's instance label alone, and flat by construction.
#: #4085 put "Review now" / "Ship now" on every card. Their enabled state is ONE bounded
#: read of the whole board's unstarted tasks, not one per card — which is precisely what
#: the two-population assertion below proves, on the page where an N+1 would be
#: multiplied by the 4s poll.
#: The health bands now name which routing scopes are PINNED to each account, which is ONE
#: bounded read of the whole pointer table built before the per-account comprehension — not
#: one per account. +1 on the two health pages, flat across both populations.
#: The health chip also dates its verdict, so ``read_health`` pays ONE ``MAX(last_seen)`` over
#: the whole registry — a second, independent +1 on the same two pages, O(1) in the population
#: and flat at both. The two land together: 21 -> 23, not 22.
PAGE_QUERY_PINS: dict[str, int] = {
    "dash:board": 12,
    "dash:board_columns": 10,
    "dash:cycle_time": 10,
    "dash:health": 23,
    "dash:health_bands": 23,
    "dash:live": 20,
    "dash:live_body": 18,
    "dash:loops": 19,
    "dash:loops_table": 19,
    "dash:presets": 14,
    "dash:sessions": 3,
    "dash:settings": 7,
    "dash:interchange": 2,
    "dash:settings_readouts": 3,
}
# #4085 added the enqueue-button row: ONE bounded read of the ticket's unstarted tasks,
# O(1) in the population. A re-pin because the drawer reads a new datum, not an N+1 —
# the two-population assertion below is what tells those apart.
TICKET_DRAWER_QUERIES = 12
TRANSCRIPT_QUERIES = 2


def _seed_task_without_dispatch(ticket: Ticket) -> Task:
    session = Session.objects.create(ticket=ticket, overlay="t3-teatree")
    return Task.objects.bulk_create([Task(ticket=ticket, session=session, phase="coding")])[0]


def _populate(scale: int) -> Ticket:
    """A dashboard's worth of rows across every model the pages read."""
    ticket = Ticket.objects.create(state=State.WORK_STARTED)
    for index in range(scale):
        each = TicketFactory(state=State.WORK_STARTED)
        task = _seed_task_without_dispatch(each)
        TaskAttempt.objects.create(
            task=task,
            model="claude-opus-4-8",
            agent_session_id=f"sess-{each.pk}",
        )
        PullRequest.objects.create(ticket=each, url=f"https://example.test/{each.pk}", repo="r", iid=str(each.pk))
        TicketTransition.objects.create(
            ticket=each, from_state=State.SCOPED, to_state=State.WORK_STARTED, triggered_by="start"
        )
        unique = f"{index}-{scale}-{uuid4().hex[:8]}"
        Loop.objects.create(name=f"loop-{unique}", delay_seconds=60, script="run.py")
        Mode.objects.create(name=f"preset-{unique}", entries={})
    return ticket


# 172.0s recorded under the shard lane's 12-way contention: 96% of the `-o timeout=180` it enforces.
@pytest.mark.timeout(240)
class DashboardPageQueryPlansTestCase(TestCase):
    """Each page's plan, asserted at a small and a large population."""

    def setUp(self) -> None:
        # The health page memoizes its spend chip for 30s, so a warm cache would make
        # the second measurement cheaper than the first and hide a plan that scales.
        # Every measurement here is the COLD plan — the one an operator pays.
        cache.clear()
        self.addCleanup(cache.clear)
        # A settings surface renders one column per registered overlay, so an exact pin
        # would otherwise measure the venue's installed packages rather than the plan.
        self.enterContext(core_overlay_only())

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
            task = _seed_task_without_dispatch(ticket)
            TaskAttempt.objects.bulk_create(TaskAttempt(task=task) for _ in range(scale))
            TicketTransition.objects.bulk_create(
                TicketTransition(
                    ticket=ticket, from_state=State.SCOPED, to_state=State.WORK_STARTED, triggered_by="start"
                )
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
