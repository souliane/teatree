"""A reopen records the merged PRs it already accounts for, so the board cannot re-merge over it (#5031)."""

from django.test import TestCase

from teatree.core.models import PullRequest, Ticket
from tests.factories import PullRequestFactory

_KEY = "reopened_over_pr_urls"


class TestReopenRecordsTheMergesItReopenedOver(TestCase):
    def _ticket_with_one_row_per_state(self) -> tuple[Ticket, list[str]]:
        ticket = Ticket.objects.create(overlay="t3-teatree", state=Ticket.State.MERGED)
        merged = [str(PullRequestFactory(ticket=ticket, state=PullRequest.State.MERGED).url) for _ in range(2)]
        PullRequestFactory(ticket=ticket, state=PullRequest.State.OPEN)
        PullRequestFactory(ticket=ticket, state=PullRequest.State.CLOSED)
        return ticket, merged

    def test_reopen_records_exactly_the_merged_pr_urls(self) -> None:
        ticket, merged = self._ticket_with_one_row_per_state()

        ticket.reopen()
        ticket.save()

        ticket.refresh_from_db()
        assert sorted(ticket.extra[_KEY]) == sorted(merged)

    def test_reopen_for_followup_records_exactly_the_merged_pr_urls(self) -> None:
        ticket, merged = self._ticket_with_one_row_per_state()

        ticket.reopen_for_followup()
        ticket.save()

        ticket.refresh_from_db()
        assert sorted(ticket.extra[_KEY]) == sorted(merged)

    def test_reopening_a_ticket_with_no_merged_row_records_an_empty_list(self) -> None:
        ticket = Ticket.objects.create(overlay="t3-teatree", state=Ticket.State.PR_OPENED)
        PullRequestFactory(ticket=ticket, state=PullRequest.State.OPEN)

        ticket.reopen()
        ticket.save()

        ticket.refresh_from_db()
        assert ticket.extra[_KEY] == []
