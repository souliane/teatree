"""Recovering the ticket link on ticketless ``MergeClear`` rows.

The walk selects on ``ticket__isnull=True``, so the link is also the walk's own
re-entry condition: a repair that persisted the link and then died would take the
row out of every later run's scope with the rest of the repair undone.
"""

from unittest.mock import patch

import pytest
from django.test import TestCase
from django.utils import timezone

from teatree.core.merge.clear_backfill import BackfillOutcome, backfill_clear_tickets
from teatree.core.models import MergeClear, PullRequest, Ticket
from teatree.core.models.pull_request import PullRequestQuerySet

_SLUG = "org/repo"
_PR_ID = 4120
_SHA = "a" * 40


class _BackfillCase(TestCase):
    def setUp(self) -> None:
        self.ticket = Ticket.objects.create(
            overlay="",
            issue_url="https://example.invalid/org/repo/issues/7",
            state=Ticket.State.SHIPPED,
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
