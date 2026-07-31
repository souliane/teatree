"""A PR closed WITHOUT merging must be recordable, and recorded when the forge says so (#3909).

``PullRequest.State`` ran OPEN → REVIEW_REQUESTED → APPROVED → MERGED with no terminal
member for "closed, never merged". Every such PR therefore stayed non-MERGED forever,
so the dashboard chip built from that row said "open" about a PR nobody will ever merge
— a chip that is worse than absent, because it reads as live work.

The forge is the authority and the teardown gate already asks it: it re-reads every
non-MERGED row's live state to decide whether a ticket is reclaimable, then threw the
answer away. Settling the row from that read is what makes the chip honest, and it also
stops a settled-on-the-forge PR being re-probed on every later teardown.
"""

import pytest
from django.test import TestCase
from django_fsm import TransitionNotAllowed

from teatree.core.backend_protocols import PrOpenState
from teatree.core.gates.open_pr_teardown_gate import open_pr_blockers
from teatree.core.models import PullRequest, Ticket, Worktree
from tests.factories import TicketFactory

State = Ticket.State
_URL = "https://github.com/acme-org/backend/pull/77"


def _row(state: str = PullRequest.State.OPEN) -> PullRequest:
    return PullRequest.objects.create(
        ticket=TicketFactory(state=State.SHIPPED),
        url=_URL,
        repo="acme-org/backend",
        iid="77",
        state=state,
    )


class ClosedIsAStateTheModelCanExpressTestCase(TestCase):
    def test_the_fsm_offers_a_closed_member(self) -> None:
        assert PullRequest.State.CLOSED in set(PullRequest.State)

    def test_an_open_pr_can_be_marked_closed(self) -> None:
        row = _row()
        row.mark_closed()
        row.save()
        row.refresh_from_db()
        assert row.state == PullRequest.State.CLOSED

    def test_a_review_requested_pr_can_be_marked_closed(self) -> None:
        row = _row(PullRequest.State.REVIEW_REQUESTED)
        row.mark_closed()
        row.save()
        assert row.state == PullRequest.State.CLOSED

    def test_a_merged_pr_can_never_be_reopened_as_closed(self) -> None:
        row = _row(PullRequest.State.MERGED)
        with pytest.raises(TransitionNotAllowed):
            row.mark_closed()


class SettlingARowFromTheForgeVerdictTestCase(TestCase):
    """``settle_forge_state`` is the one writer that turns a live read into a durable row."""

    def test_a_closed_verdict_settles_the_row(self) -> None:
        row = _row()
        assert PullRequest.objects.settle_forge_state(row, PrOpenState.CLOSED) is True
        row.refresh_from_db()
        assert row.state == PullRequest.State.CLOSED

    def test_a_merged_verdict_settles_the_row(self) -> None:
        row = _row()
        assert PullRequest.objects.settle_forge_state(row, PrOpenState.MERGED) is True
        row.refresh_from_db()
        assert row.state == PullRequest.State.MERGED

    def test_an_open_verdict_changes_nothing(self) -> None:
        row = _row()
        assert PullRequest.objects.settle_forge_state(row, PrOpenState.OPEN) is False
        row.refresh_from_db()
        assert row.state == PullRequest.State.OPEN

    def test_an_unknown_verdict_never_settles_a_row(self) -> None:
        row = _row()
        assert PullRequest.objects.settle_forge_state(row, PrOpenState.UNKNOWN) is False
        row.refresh_from_db()
        assert row.state == PullRequest.State.OPEN

    def test_settling_is_idempotent(self) -> None:
        row = _row()
        PullRequest.objects.settle_forge_state(row, PrOpenState.CLOSED)
        assert PullRequest.objects.settle_forge_state(row, PrOpenState.CLOSED) is False


class TheTeardownGateKeepsTheAnswerItAlreadyPaidForTestCase(TestCase):
    """The gate re-reads every non-MERGED row live; that verdict is the row's settle."""

    def _worktrees(self, ticket: Ticket) -> list[Worktree]:
        return [
            Worktree.objects.create(
                ticket=ticket,
                overlay="t3-teatree",
                repo_path="/nonexistent/repo",
                extra={"worktree_path": "/nonexistent/repo/wt"},
                branch="some-branch",
            )
        ]

    def test_a_closed_verdict_is_recorded_on_the_row(self) -> None:
        row = _row()
        open_pr_blockers(row.ticket, self._worktrees(row.ticket), read_pr_state=lambda pr_url: PrOpenState.CLOSED)
        row.refresh_from_db()
        assert row.state == PullRequest.State.CLOSED

    def test_a_merged_verdict_is_recorded_on_the_row(self) -> None:
        row = _row()
        open_pr_blockers(row.ticket, self._worktrees(row.ticket), read_pr_state=lambda pr_url: PrOpenState.MERGED)
        row.refresh_from_db()
        assert row.state == PullRequest.State.MERGED

    def test_an_open_verdict_still_blocks_and_leaves_the_row_alone(self) -> None:
        row = _row()
        blockers = open_pr_blockers(
            row.ticket,
            self._worktrees(row.ticket),
            read_pr_state=lambda pr_url: PrOpenState.OPEN,
        )
        row.refresh_from_db()
        assert row.state == PullRequest.State.OPEN
        assert any("still OPEN" in blocker for blocker in blockers)
