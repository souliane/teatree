"""Two agents must not both hold one checkout (souliane/teatree#3952).

The guard the tests below pin is a compare-and-swap, so the assertions are on the
*decision* each acquisition returns — not on a post-condition the buggy code also
produces. Remove the ``grantable`` predicate from
:func:`teatree.core.worktree.occupancy.acquire` and every overlapping-acquisition
test here goes RED (both requesters win); a test asserting only "the row names
someone" would stay green against exactly that defect.
"""

from datetime import timedelta
from pathlib import Path

import pytest
from django.test import TestCase
from django.utils import timezone

from teatree.core.models import Worktree
from teatree.core.worktree.occupancy import (
    WorktreeOccupancyLostError,
    WorktreeOccupiedError,
    acquire,
    held_worktrees,
    occupancy_holder,
    occupy_ticket_checkout,
    refuse_if_ticket_checkout_occupied,
    release,
    renew,
    task_holder_id,
)
from tests.factories import TaskFactory, TicketFactory, WorktreeFactory


class _OccupancyCase(TestCase):
    """One registered worktree with a real on-disk checkout dir."""

    def setUp(self) -> None:
        super().setUp()
        self.ticket = TicketFactory()
        self.checkout = Path(self.mktemp())
        self.worktree = WorktreeFactory(ticket=self.ticket, extra={"worktree_path": str(self.checkout)})

    def mktemp(self) -> str:
        import tempfile  # noqa: PLC0415 — test-local, keeps the module import graph flat

        path = tempfile.mkdtemp()
        self.addCleanup(lambda: Path(path).exists() and Path(path).rmdir())
        return path

    def fresh(self) -> Worktree:
        return Worktree.objects.get(pk=self.worktree.pk)


class AcquireTests(_OccupancyCase):
    def test_second_overlapping_acquisition_is_refused(self) -> None:
        acquire(self.worktree, holder="task:1", holder_session="s1")
        with pytest.raises(WorktreeOccupiedError):
            acquire(self.fresh(), holder="task:2", holder_session="s2")

    def test_refusal_names_the_holder_and_the_release_command(self) -> None:
        acquire(self.worktree, holder="task:1", holder_session="session-abc")
        with pytest.raises(WorktreeOccupiedError) as caught:
            acquire(self.fresh(), holder="task:2", holder_session="s2")
        message = str(caught.value)
        assert "task:1" in message
        assert "session-abc" in message
        assert str(self.checkout) in message
        assert "worktree release-occupancy" in message
        assert caught.value.holder is not None
        assert caught.value.holder.holder == "task:1"

    def test_the_first_holder_keeps_the_claim_after_a_refusal(self) -> None:
        acquire(self.worktree, holder="task:1", holder_session="s1")
        with pytest.raises(WorktreeOccupiedError):
            acquire(self.fresh(), holder="task:2", holder_session="s2")
        held = occupancy_holder(self.fresh())
        assert held is not None
        assert held.holder == "task:1"

    def test_same_holder_reacquire_refreshes_rather_than_refuses(self) -> None:
        acquire(self.worktree, holder="task:1", holder_session="s1", lease_seconds=60)
        first = self.fresh().occupancy_expires_at
        acquire(self.fresh(), holder="task:1", holder_session="s1", lease_seconds=600)
        second = self.fresh().occupancy_expires_at
        assert first is not None
        assert second is not None
        assert second > first

    def test_a_different_session_of_the_same_holder_is_still_a_rival(self) -> None:
        acquire(self.worktree, holder="task:1", holder_session="s1")
        with pytest.raises(WorktreeOccupiedError):
            acquire(self.fresh(), holder="task:1", holder_session="s2")

    def test_a_lapsed_lease_is_grantable_to_the_next_requester(self) -> None:
        acquire(self.worktree, holder="task:1", holder_session="s1", lease_seconds=60)
        Worktree.objects.filter(pk=self.worktree.pk).update(occupancy_expires_at=timezone.now() - timedelta(seconds=1))
        acquire(self.fresh(), holder="task:2", holder_session="s2")
        held = occupancy_holder(self.fresh())
        assert held is not None
        assert held.holder == "task:2"

    def test_acquiring_writes_nothing_to_disk(self) -> None:
        before = sorted(p.name for p in self.checkout.iterdir())
        acquire(self.worktree, holder="task:1", holder_session="s1")
        with pytest.raises(WorktreeOccupiedError):
            acquire(self.fresh(), holder="task:2", holder_session="s2")
        assert self.checkout.is_dir()
        assert sorted(p.name for p in self.checkout.iterdir()) == before


class ReleaseTests(_OccupancyCase):
    def test_release_frees_the_checkout_for_the_next_requester(self) -> None:
        acquire(self.worktree, holder="task:1", holder_session="s1")
        assert release(self.fresh(), holder="task:1", holder_session="s1") is True
        assert occupancy_holder(self.fresh()) is None
        acquire(self.fresh(), holder="task:2", holder_session="s2")

    def test_a_foreign_release_never_steals_the_claim(self) -> None:
        acquire(self.worktree, holder="task:1", holder_session="s1")
        assert release(self.fresh(), holder="task:2", holder_session="s2") is False
        held = occupancy_holder(self.fresh())
        assert held is not None
        assert held.holder == "task:1"

    def test_releasing_an_unheld_checkout_is_a_no_op(self) -> None:
        assert release(self.worktree, holder="task:1", holder_session="s1") is False


class RenewTests(_OccupancyCase):
    def test_renew_extends_this_holders_lease(self) -> None:
        acquire(self.worktree, holder="task:1", holder_session="s1", lease_seconds=60)
        held = self.fresh()
        first = held.occupancy_expires_at
        renew(held, lease_seconds=600)
        assert first is not None
        assert held.occupancy_expires_at is not None
        assert held.occupancy_expires_at > first

    def test_renew_after_a_takeover_reports_the_loss(self) -> None:
        acquire(self.worktree, holder="task:1", holder_session="s1", lease_seconds=60)
        stale = self.fresh()
        Worktree.objects.filter(pk=self.worktree.pk).update(occupancy_expires_at=timezone.now() - timedelta(seconds=1))
        acquire(self.fresh(), holder="task:2", holder_session="s2")
        with pytest.raises(WorktreeOccupancyLostError):
            renew(stale)

    def test_renew_without_a_claim_reports_the_loss(self) -> None:
        with pytest.raises(WorktreeOccupancyLostError):
            renew(self.worktree)


class TicketCheckoutTests(_OccupancyCase):
    def test_the_context_manager_yields_the_checkout_and_releases_it(self) -> None:
        with occupy_ticket_checkout(self.ticket, holder="task:1", holder_session="s1") as path:
            assert path == str(self.checkout)
            with pytest.raises(WorktreeOccupiedError):
                acquire(self.fresh(), holder="task:2", holder_session="s2")
        assert occupancy_holder(self.fresh()) is None

    def test_the_claim_is_released_when_the_body_raises(self) -> None:
        sentinel = RuntimeError("boom")
        with (
            pytest.raises(RuntimeError, match="boom"),
            occupy_ticket_checkout(self.ticket, holder="task:1", holder_session="s1"),
        ):
            raise sentinel
        assert occupancy_holder(self.fresh()) is None

    def test_two_overlapping_ticket_requests_do_not_both_get_the_checkout(self) -> None:
        with (
            occupy_ticket_checkout(self.ticket, holder="task:1", holder_session="s1"),
            pytest.raises(WorktreeOccupiedError),
            occupy_ticket_checkout(self.ticket, holder="task:2", holder_session="s2"),
        ):
            pass

    def test_a_ticket_with_no_materialised_checkout_claims_nothing(self) -> None:
        bare = TicketFactory()
        with occupy_ticket_checkout(bare, holder="task:1", holder_session="s1") as path:
            assert path == ""

    def test_the_kill_switch_hands_out_the_checkout_ungated(self) -> None:
        acquire(self.worktree, holder="task:1", holder_session="s1")
        with occupy_ticket_checkout(self.ticket, holder="task:2", holder_session="s2", enabled=False) as path:
            assert path == str(self.checkout)
        held = occupancy_holder(self.fresh())
        assert held is not None
        assert held.holder == "task:1"


class RefuseIfOccupiedTests(_OccupancyCase):
    def test_a_free_checkout_is_handed_over(self) -> None:
        refuse_if_ticket_checkout_occupied(self.ticket)

    def test_an_occupied_checkout_is_refused_naming_the_holder(self) -> None:
        acquire(self.worktree, holder="task:7", holder_session="s7")
        with pytest.raises(WorktreeOccupiedError, match="task:7"):
            refuse_if_ticket_checkout_occupied(self.ticket)

    def test_the_refusal_takes_no_claim_of_its_own(self) -> None:
        acquire(self.worktree, holder="task:7", holder_session="s7")
        with pytest.raises(WorktreeOccupiedError):
            refuse_if_ticket_checkout_occupied(self.ticket)
        held = occupancy_holder(self.fresh())
        assert held is not None
        assert held.holder == "task:7"

    def test_a_lapsed_claim_no_longer_refuses(self) -> None:
        acquire(self.worktree, holder="task:7", holder_session="s7", lease_seconds=60)
        Worktree.objects.filter(pk=self.worktree.pk).update(occupancy_expires_at=timezone.now() - timedelta(seconds=1))
        refuse_if_ticket_checkout_occupied(self.ticket)


class HeldWorktreeReportTests(_OccupancyCase):
    def test_only_live_claims_are_reported(self) -> None:
        acquire(self.worktree, holder="task:7", holder_session="s7", lease_seconds=60)
        assert [holder.holder for _, holder in held_worktrees()] == ["task:7"]
        Worktree.objects.filter(pk=self.worktree.pk).update(occupancy_expires_at=timezone.now() - timedelta(seconds=1))
        assert held_worktrees() == []


class HolderIdTests(TestCase):
    def test_the_task_holder_id_is_stable_and_qualified(self) -> None:
        task = TaskFactory()
        assert task_holder_id(task) == f"task:{task.pk}"
