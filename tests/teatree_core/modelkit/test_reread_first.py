"""A task minted from a hold re-reads the branch and the issue before it acts."""

from django.test import TestCase

from teatree.core.modelkit.reread_first import REREAD_FIRST_CLAUSE
from teatree.core.models import Ticket
from tests.factories import planned_ticket

_HOLD = {"reason": "worktree dirty", "skipped": "shipping", "held_at": "2026-10-31", "checked_at": "2026-10-31"}


class TestHoldMintedTaskRereadsFirst(TestCase):
    def test_a_task_minted_from_a_shipping_hold_carries_the_clause(self) -> None:
        ticket = planned_ticket(
            role=Ticket.Role.AUTHOR, state=Ticket.State.WORK_STARTED, extra={"shipping_hold": _HOLD}
        )
        task = ticket.schedule_implementing("debugging", reason="Auto-scheduled fix")
        assert task.execution_reason.startswith("Auto-scheduled fix")
        assert REREAD_FIRST_CLAUSE in task.execution_reason

    def test_a_task_minted_without_a_hold_is_unchanged(self) -> None:
        ticket = planned_ticket(role=Ticket.Role.AUTHOR, state=Ticket.State.WORK_STARTED)
        task = ticket.schedule_implementing("debugging", reason="Auto-scheduled fix")
        assert task.execution_reason == "Auto-scheduled fix"

    def test_the_clause_names_the_branch_the_issue_and_the_already_fixed_return(self) -> None:
        for needle in ("branch", "issue", "already_fixed", "verified_head"):
            assert needle in REREAD_FIRST_CLAUSE
