"""The invariant #4152 pins: a reopened issue is never permanently invisible to every path.

A ticket owns its issue URL in every state but IGNORED, and ``get_or_create(issue_url=...)``
makes a second ticket for the same issue structurally impossible — so before this, a DELIVERED
ticket owned its issue forever and a reopened issue behind it reached NOTHING: not intake,
not the board reconcile's PR rules, not the stuck sweep (a delivered ticket is not stuck).

This lane walks the whole valve rather than one rule: DELIVERED + a forge REOPENED verdict →
the board revives the ticket → the hard-bounded stuck-redispatch sweep schedules real work.
Each half alone would pass on a chain that still dead-ends, which is why they are asserted
together.
"""

import contextlib
from collections.abc import Iterator
from datetime import timedelta
from unittest.mock import patch

from django.test import TestCase
from django.utils import timezone
from django_fsm import can_proceed

from teatree.core.backend_protocols import IssueReopenState
from teatree.core.models import Task, Ticket
from teatree.core.models.transition import TicketTransition
from teatree.loop.scanners import board_reconcile
from teatree.loop.scanners.board_reconcile import reconcile_board
from teatree.loop.stuck_ticket_redispatch import redispatch_stuck_tickets

_URL = "https://github.com/souliane/teatree/issues/4152"


@contextlib.contextmanager
def _forge_says_reopened() -> Iterator[None]:
    with (
        patch("teatree.core.overlay_loader.get_all_overlays", return_value={"t3-teatree": object()}),
        patch.object(board_reconcile, "issue_reopen_state", return_value=IssueReopenState.REOPENED),
    ):
        yield


def _age_activity(ticket: Ticket) -> None:
    """Backdate the ticket's transitions past the stuck sweep's idle threshold.

    ``created_at`` is ``auto_now_add``, so a queryset ``update`` is the only way to age it —
    and the revival itself writes a fresh transition, which is what would otherwise keep a
    just-revived ticket out of the sweep for the threshold's duration.
    """
    TicketTransition.objects.filter(ticket=ticket).update(created_at=timezone.now() - timedelta(days=1))


class TestDeliveredIsNotTheEndOfEveryPath(TestCase):
    def test_the_fsm_carries_a_release_valve_out_of_delivered(self) -> None:
        ticket = Ticket.objects.create(overlay="t3-teatree", state=Ticket.State.DELIVERED, issue_url=_URL)

        assert can_proceed(ticket.reopen)

    def test_a_reopened_issue_behind_a_delivered_ticket_ends_up_with_work_scheduled(self) -> None:
        ticket = Ticket.objects.create(overlay="t3-teatree", state=Ticket.State.DELIVERED, issue_url=_URL)

        with _forge_says_reopened():
            reconcile_board()

        ticket.refresh_from_db()
        assert ticket.state == Ticket.State.STARTED

        _age_activity(ticket)
        assert redispatch_stuck_tickets() == 1

        assert list(ticket.tasks.values_list("phase", flat=True)) == ["planning"]
        assert ticket.tasks.filter(status__in=Task.Status.active()).exists()
