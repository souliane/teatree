"""Recovering the ticket link on ticketless ``MergeClear`` rows.

The walk selects on ``ticket__isnull=True``, so the link is also the walk's own
re-entry condition: a repair that persisted the link and then died would take the
row out of every later run's scope with the rest of the repair undone.
"""

from unittest.mock import patch

import pytest
from django.test import TestCase
from django.utils import timezone

from teatree.backends import forge_merge_rpc
from teatree.backends.github import client as github_client
from teatree.core.merge.clear_backfill import BackfillOutcome, backfill_clear_tickets
from teatree.core.models import MergeAudit, MergeClear, PullRequest, Task, Ticket
from teatree.core.models.pull_request import PullRequestQuerySet
from tests.teatree_core.merge.partial_merge_helpers import pr_bodies

_SLUG = "org/repo"
_PR_ID = 4120
_SHA = "a" * 40


class _BackfillCase(TestCase):
    def setUp(self) -> None:
        self.ticket = Ticket.objects.create(
            overlay="",
            issue_url="https://example.invalid/org/repo/issues/7",
            state=Ticket.State.PR_OPENED,
        )
        PullRequest.objects.create(
            overlay="",
            ticket=self.ticket,
            url=f"https://example.invalid/{_SLUG}/pull/{_PR_ID}",
            repo=_SLUG,
            iid=str(_PR_ID),
        )

    def _clear(self, *, consumed: bool = True) -> MergeClear:
        return MergeClear.objects.create(
            ticket=None,
            slug=_SLUG,
            pr_id=_PR_ID,
            reviewed_sha=_SHA,
            reviewer_identity="reviewer",
            gh_verify_result=MergeClear.VerifyResult.GREEN,
            blast_class=MergeClear.BlastClass.LOGIC,
            consumed_at=timezone.now() if consumed else None,
        )


class LinksAConsumedClearTest(_BackfillCase):
    def test_a_consumed_clear_is_linked_to_the_pr_s_ticket(self) -> None:
        clear = self._clear()

        report = backfill_clear_tickets()

        clear.refresh_from_db()
        assert clear.ticket_id == self.ticket.pk
        assert [row.outcome for row in report.rows] == [BackfillOutcome.LINKED]

    def test_an_unconsumed_clear_is_left_as_a_live_authorisation(self) -> None:
        clear = self._clear(consumed=False)

        report = backfill_clear_tickets()

        clear.refresh_from_db()
        assert clear.ticket_id is None
        assert [row.outcome for row in report.rows] == [BackfillOutcome.LIVE]

    def test_a_dry_run_persists_nothing(self) -> None:
        clear = self._clear()

        report = backfill_clear_tickets(dry_run=True)

        clear.refresh_from_db()
        assert clear.ticket_id is None
        assert report.dry_run is True


class RepairIsAllOrNothingTest(_BackfillCase):
    """A repair interrupted partway must stay re-selectable by the next run."""

    def test_a_failure_after_linking_leaves_the_clear_re_selectable(self) -> None:
        clear = self._clear()

        with (
            patch.object(PullRequestQuerySet, "record_forge_merge", side_effect=RuntimeError("forge write died")),
            pytest.raises(RuntimeError),
        ):
            backfill_clear_tickets()

        clear.refresh_from_db()
        assert clear.ticket_id is None, "a half-applied repair takes the row out of `ticket__isnull=True` forever"
        assert list(MergeClear.objects.filter(ticket__isnull=True)) == [clear]


class HoldsAMergeThatLeavesAnItemOutTest(TestCase):
    def _consumed_clear(self, body: str, *, issue: dict[str, str] | Exception) -> tuple[Ticket, MergeClear]:
        ticket = Ticket.objects.create(
            overlay="t3-teatree",
            issue_url="https://github.com/souliane/teatree/issues/4242",
            state=Ticket.State.PR_OPENED,
        )
        PullRequest.objects.create(
            overlay="t3-teatree",
            ticket=ticket,
            url="https://github.com/souliane/teatree/pull/859",
            repo="souliane/teatree",
            iid="859",
        )
        clear = MergeClear.objects.create(
            slug="souliane/teatree",
            pr_id=859,
            host_kind="github",
            reviewed_sha=_SHA,
            reviewer_identity="reviewer",
            gh_verify_result=MergeClear.VerifyResult.GREEN,
            blast_class=MergeClear.BlastClass.LOGIC,
            consumed_at=timezone.now(),
        )
        MergeAudit.objects.create(clear=clear, merged_sha="b" * 40, repo_slug="souliane/teatree")
        with (
            patch.object(forge_merge_rpc, "gh_runner", return_value=pr_bodies({"859": body})),
            patch.object(
                github_client.GitHubCodeHost,
                "get_issue",
                side_effect=issue if isinstance(issue, Exception) else lambda _url: issue,
            ),
        ):
            backfill_clear_tickets()
        ticket.refresh_from_db()
        return ticket, clear

    def test_a_merge_that_leaves_an_item_out_sends_the_ticket_back_to_planning(self) -> None:
        issue = {"state": "open", "body": "## Acceptance\n- The first thing\n- The second thing\n"}

        ticket, _ = self._consumed_clear("Part of #4242\n\n- The first thing\n", issue=issue)

        assert ticket.state == Ticket.State.WORK_STARTED
        (task,) = Task.objects.filter(ticket=ticket, phase="planning")
        assert "The second thing" in task.execution_reason

    def test_a_merge_that_quotes_every_item_advances_the_ticket(self) -> None:
        issue = {"state": "open", "body": "## Acceptance\n- The first thing\n- The second thing\n"}

        ticket, _ = self._consumed_clear("Part of #4242\n\n- The first thing\n- The second thing\n", issue=issue)

        assert ticket.state == Ticket.State.MERGED

    def test_a_merge_the_forge_cannot_read_leaves_the_ticket_where_it_was(self) -> None:
        ticket, _ = self._consumed_clear("Part of #4242\n", issue=RuntimeError("forge down"))

        assert ticket.state == Ticket.State.PR_OPENED
