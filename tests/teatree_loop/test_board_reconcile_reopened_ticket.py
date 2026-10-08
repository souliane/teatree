"""Board-reconcile rule A leaves a reopened ticket reopened (#5031).

A ticket reopened after its PR merged kept a MERGED ``PullRequest`` row, and rule A
re-merged it on the next tick (ticket 1672: reopened at 10:07:52.28, re-merged at
10:07:53.75), re-enqueuing its worktree teardown each time. Every lane here pairs the
reopened ticket with a never-reopened control in the SAME pass that does reach MERGED,
so a rule that never ran cannot pass.
"""

from unittest.mock import patch

from django.test import TestCase

from teatree.core.models import PullRequest, Ticket
from teatree.loop.scanners import board_reconcile_issue_reopen as issue_reopen
from teatree.loop.scanners.board_reconcile import reconcile_board
from tests.factories import PullRequestFactory
from tests.teatree_loop._board_reconcile_overlays import rule_f_inert
from tests.teatree_loop._reopened_tickets import merged_ticket, reopened_ticket


def _applied_ids(*, dry_run: bool = False) -> list[int]:
    report = reconcile_board(probe_forge=False, dry_run=dry_run)
    return [t.ticket_id for t in (report.transitions if dry_run else report.applied)]


class TestAReopenedTicketStaysReopened(TestCase):
    def _assert_stays_reopened(self, reopened: Ticket, *, state: str) -> None:
        merges_before = reopened.transitions.filter(to_state=Ticket.State.MERGED).count()
        control = merged_ticket(state=Ticket.State.NOT_STARTED)

        assert _applied_ids() == [control.pk]

        reopened.refresh_from_db()
        control.refresh_from_db()
        assert reopened.state == state
        assert reopened.transitions.filter(to_state=Ticket.State.MERGED).count() == merges_before
        assert control.state == Ticket.State.MERGED
        assert control.transitions.filter(to_state=Ticket.State.MERGED).exists()

    def test_a_reopen_survives_the_tick_pass(self) -> None:
        self._assert_stays_reopened(reopened_ticket(), state=Ticket.State.WORK_STARTED)

    def test_a_followup_reopen_survives_the_tick_pass(self) -> None:
        self._assert_stays_reopened(reopened_ticket(followup=True), state=Ticket.State.SELF_REVIEWED)

    def test_the_stamp_survives_a_rework(self) -> None:
        ticket = reopened_ticket(followup=True)
        ticket.rework()
        ticket.save()

        self._assert_stays_reopened(ticket, state=Ticket.State.WORK_STARTED)

    def test_an_unreadable_stamp_skips_its_own_ticket_and_the_pass_goes_on(self) -> None:
        ticket = reopened_ticket()
        Ticket.objects.filter(pk=ticket.pk).update(extra={**ticket.extra, "reopened_over_pr_urls": 7})

        self._assert_stays_reopened(ticket, state=Ticket.State.WORK_STARTED)

    def test_dry_run_plans_exactly_what_the_pass_applies(self) -> None:
        reopened_ticket()
        control = merged_ticket(state=Ticket.State.NOT_STARTED)

        planned = _applied_ids(dry_run=True)
        applied = _applied_ids()

        assert planned == applied == [control.pk]

    def test_a_pr_linked_after_the_reopen_advances_the_ticket_when_it_merges(self) -> None:
        reopened = reopened_ticket()
        control = merged_ticket(state=Ticket.State.NOT_STARTED)
        later = PullRequestFactory(ticket=reopened, state=PullRequest.State.OPEN)

        assert _applied_ids() == [control.pk]

        PullRequest.objects.filter(pk=later.pk).update(state=PullRequest.State.MERGED)

        assert _applied_ids() == [reopened.pk]
        reopened.refresh_from_db()
        assert reopened.state == Ticket.State.MERGED

    def test_a_pr_open_at_the_reopen_that_merges_later_still_advances(self) -> None:
        ticket = merged_ticket(state=Ticket.State.PR_OPENED)
        PullRequest.objects.filter(ticket=ticket).update(state=PullRequest.State.OPEN)
        ticket.reopen()
        ticket.save()

        PullRequest.objects.filter(ticket=ticket).update(state=PullRequest.State.MERGED)

        assert _applied_ids() == [ticket.pk]
        ticket.refresh_from_db()
        assert ticket.state == Ticket.State.MERGED

    def test_a_second_reopen_covers_the_pr_that_merged_in_between(self) -> None:
        ticket = reopened_ticket()
        PullRequestFactory(ticket=ticket, state=PullRequest.State.MERGED)
        assert _applied_ids() == [ticket.pk]
        ticket.refresh_from_db()
        assert ticket.state == Ticket.State.MERGED

        ticket.reopen()
        ticket.save()

        self._assert_stays_reopened(ticket, state=Ticket.State.WORK_STARTED)

    def test_a_rule_e_revival_survives_the_next_tick(self) -> None:
        url = "https://github.com/souliane/teatree/issues/4133"
        ticket = merged_ticket(state=Ticket.State.DELIVERED, issue_url=url)

        with patch.object(issue_reopen, "_reopened_issue_urls", return_value=({url}, 1)), rule_f_inert():
            reconcile_board()
        ticket.refresh_from_db()
        assert (ticket.state, ticket.extra["reopen_revivals"]) == (Ticket.State.WORK_STARTED, 1)

        self._assert_stays_reopened(ticket, state=Ticket.State.WORK_STARTED)
        assert ticket.extra["reopen_revivals"] == 1
