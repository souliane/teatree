"""Two agents must not both hold one checkout (souliane/teatree#3952).

The guard the tests below pin is a compare-and-swap, so the assertions are on the
*decision* each acquisition returns — not on a post-condition the buggy code also
produces. Remove the ``grantable`` predicate from
:func:`teatree.core.worktree.occupancy.acquire` and every overlapping-acquisition
test here goes RED (both requesters win); a test asserting only "the row names
someone" would stay green against exactly that defect.
"""

from collections.abc import Callable
from datetime import timedelta
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

import pytest
from django.test import TestCase
from django.utils import timezone

from teatree.core.cleanup.process_table import ProcessTable
from teatree.core.models import Task, Worktree
from teatree.core.models.task_claim import drive_claim
from teatree.core.worktree.checkout_state import checkout_state
from teatree.core.worktree.occupancy import (
    WorktreeOccupancyLostError,
    WorktreeOccupiedError,
    acquire,
    held_worktrees,
    occupancy_holder,
    occupy_ticket_checkout,
    refuse_if_ticket_checkout_occupied,
    release,
    renew_ticket_checkout,
    task_holder_id,
)
from tests.factories import TaskFactory, TicketFactory, WorktreeFactory
from tests.teatree_core.models._shared import _init_repo_with_branch


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
    def test_the_heartbeat_extends_this_holders_lease(self) -> None:
        acquire(self.worktree, holder="task:1", holder_session="s1", lease_seconds=60)
        first = self.fresh().occupancy_expires_at
        renew_ticket_checkout(self.ticket, holder="task:1", holder_session="s1", lease_seconds=600)
        second = self.fresh().occupancy_expires_at
        assert first is not None
        assert second is not None
        assert second > first

    def test_the_heartbeat_retakes_a_lease_that_lapsed_while_still_unclaimed(self) -> None:
        acquire(self.worktree, holder="task:1", holder_session="s1", lease_seconds=60)
        Worktree.objects.filter(pk=self.worktree.pk).update(occupancy_expires_at=timezone.now() - timedelta(seconds=1))
        renew_ticket_checkout(self.ticket, holder="task:1", holder_session="s1")
        held = occupancy_holder(self.fresh())
        assert held is not None
        assert held.holder == "task:1"

    def test_the_heartbeat_reports_the_loss_once_a_rival_took_over(self) -> None:
        acquire(self.worktree, holder="task:1", holder_session="s1", lease_seconds=60)
        Worktree.objects.filter(pk=self.worktree.pk).update(occupancy_expires_at=timezone.now() - timedelta(seconds=1))
        acquire(self.fresh(), holder="task:2", holder_session="s2")
        with pytest.raises(WorktreeOccupancyLostError, match=str(self.checkout)):
            renew_ticket_checkout(self.ticket, holder="task:1", holder_session="s1")

    def test_the_heartbeat_mints_no_claim_for_a_ticket_with_no_checkout(self) -> None:
        Worktree.objects.filter(pk=self.worktree.pk).delete()
        renew_ticket_checkout(self.ticket, holder="task:1", holder_session="s1")
        assert held_worktrees() == []


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


class MidRunCheckoutTests(TestCase):
    """A checkout that appears during the run is settled by the run's own exit (#5272)."""

    def setUp(self) -> None:
        super().setUp()
        temporary = TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.checkout = Path(temporary.name) / "checkout"
        self.ticket = TicketFactory()
        self.task = TaskFactory(ticket=self.ticket, phase="coding")
        self.task.claim(claimed_by="task-worker")
        self.worktree = WorktreeFactory(ticket=self.ticket, extra={"worktree_path": str(self.checkout)})

    def run_to(
        self, finish: Callable[[Task], None], *, creates_checkout: bool = True, held_at_exit: bool = False
    ) -> None:
        holder, session = task_holder_id(self.task), self.task.claimed_by_session
        table = ProcessTable(frozenset({self.checkout}) if held_at_exit else frozenset(), "/proc")
        with (
            patch(
                "teatree.core.worktree.occupancy.read_process_table",
                return_value=table,
            ),
            occupy_ticket_checkout(self.ticket, holder=holder, holder_session=session) as path,
            drive_claim(self.task),
        ):
            assert path == ""
            if creates_checkout:
                _init_repo_with_branch(self.checkout, branch="feature", commits_ahead=0)
                renew_ticket_checkout(self.ticket, holder=holder, holder_session=session)
                (self.checkout / "work.txt").write_text("the run's own edit\n")
            finish(self.task)

    def assert_released_and_attested(self) -> None:
        finished = Worktree.objects.get(pk=self.worktree.pk)
        assert occupancy_holder(finished) is None
        assert finished.get_extra()["attested_state"] == checkout_state(self.checkout).digest
        task = Task.objects.get(pk=self.task.pk)
        assert (task.claimed_by, task.owner_pid) == ("", None)

    def test_completion_releases_and_attests_a_checkout_taken_mid_run(self) -> None:
        self.run_to(lambda task: task.complete())
        self.assert_released_and_attested()

    def test_a_failed_run_releases_and_attests_a_checkout_taken_mid_run(self) -> None:
        self.run_to(lambda task: task.fail_claimed(reason="result_error: harness ended"))
        self.assert_released_and_attested()

    def test_a_run_that_never_gets_a_checkout_releases_its_claim_and_writes_no_row(self) -> None:
        self.run_to(lambda task: task.complete(), creates_checkout=False)
        assert Task.objects.get(pk=self.task.pk).claimed_by == ""
        row = Worktree.objects.get(pk=self.worktree.pk)
        assert row.occupied_by == ""
        assert "attested_state" not in row.get_extra()

    def test_held_exit_records_the_state_it_would_have_attested(self) -> None:
        self.run_to(lambda task: task.complete(), held_at_exit=True)
        row = Worktree.objects.get(pk=self.worktree.pk)
        record = row.get_extra()["deferred_exit"]
        assert record["holder"] == task_holder_id(self.task)
        assert record["session"] == self.task.claimed_by_session
        assert record["digest"] == checkout_state(self.checkout, ticket=self.ticket).digest
        assert record["cause"] == "process_table"
        assert record["at"]
        assert row.occupied_by == task_holder_id(self.task)
        assert "attested_state" not in row.get_extra()

    def test_own_process_survivor_records_its_deferral_cause(self) -> None:
        with patch("teatree.core.worktree.occupancy.wait_for_owned_checkout_processes", return_value=[object()]):
            self.run_to(lambda task: task.complete())
        row = Worktree.objects.get(pk=self.worktree.pk)
        assert row.get_extra()["deferred_exit"]["cause"] == "own_processes"

    def test_read_only_exit_records_no_deferred_proof(self) -> None:
        self.task.phase = "reviewing"
        self.task.save(update_fields=["phase"])
        self.run_to(lambda task: task.complete(), held_at_exit=True)
        row = Worktree.objects.get(pk=self.worktree.pk)
        assert "deferred_exit" not in row.get_extra()

    def test_unreadable_exit_state_records_no_deferred_proof(self) -> None:
        with patch("teatree.core.worktree.occupancy.checkout_state", side_effect=RuntimeError("unreadable")):
            self.run_to(lambda task: task.complete(), held_at_exit=True)
        row = Worktree.objects.get(pk=self.worktree.pk)
        assert "deferred_exit" not in row.get_extra()


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


class StaleFinishedHolderTests(_OccupancyCase):
    """A claim named by a Task that already finished self-heals rather than refuses (#4867).

    The two REAL terminal-status writers (``complete_claimed``, ``Task.fail``) now
    release inline; this covers every row that bypasses them (an out-of-process
    writer, a pre-#4867 row) via the self-heal in ``occupy_ticket_checkout`` and
    ``refuse_if_ticket_checkout_occupied``. Only a DEFINITIVE terminal ``Task.status``
    read triggers it — a live holder's claim must never be touched.
    """

    def test_occupy_ticket_checkout_self_heals_a_finished_holder(self) -> None:
        finished = TaskFactory(status=Task.Status.COMPLETED)
        acquire(self.worktree, holder=task_holder_id(finished), holder_session="s1")
        with occupy_ticket_checkout(self.ticket, holder="task:2", holder_session="s2") as path:
            assert path == str(self.checkout)
        assert occupancy_holder(self.fresh()) is None

    def test_occupy_ticket_checkout_still_refuses_a_live_holder(self) -> None:
        live = TaskFactory(status=Task.Status.PENDING)
        acquire(self.worktree, holder=task_holder_id(live), holder_session="s1")
        with (
            pytest.raises(WorktreeOccupiedError),
            occupy_ticket_checkout(self.ticket, holder="task:2", holder_session="s2"),
        ):
            pass
        held = occupancy_holder(self.fresh())
        assert held is not None
        assert held.holder == task_holder_id(live)

    def test_refuse_if_occupied_self_heals_a_finished_holder(self) -> None:
        finished = TaskFactory(status=Task.Status.FAILED)
        acquire(self.worktree, holder=task_holder_id(finished), holder_session="s1")
        refuse_if_ticket_checkout_occupied(self.ticket)
        assert occupancy_holder(self.fresh()) is None

    def test_refuse_if_occupied_still_refuses_a_live_holder(self) -> None:
        live = TaskFactory(status=Task.Status.CLAIMED)
        acquire(self.worktree, holder=task_holder_id(live), holder_session="s1")
        with pytest.raises(WorktreeOccupiedError, match=task_holder_id(live)):
            refuse_if_ticket_checkout_occupied(self.ticket)
        held = occupancy_holder(self.fresh())
        assert held is not None
        assert held.holder == task_holder_id(live)

    def test_a_non_task_holder_is_never_self_healed(self) -> None:
        acquire(self.worktree, holder="operator:someone", holder_session="s1")
        with pytest.raises(WorktreeOccupiedError):
            refuse_if_ticket_checkout_occupied(self.ticket)
        held = occupancy_holder(self.fresh())
        assert held is not None
        assert held.holder == "operator:someone"


class HeldWorktreeReportTests(_OccupancyCase):
    def test_only_live_claims_are_reported(self) -> None:
        acquire(self.worktree, holder="task:7", holder_session="s7", lease_seconds=60)
        assert [holder.holder for _, holder in held_worktrees()] == ["task:7"]
        Worktree.objects.filter(pk=self.worktree.pk).update(occupancy_expires_at=timezone.now() - timedelta(seconds=1))
        assert held_worktrees() == []


class LivenessAgreementTests(_OccupancyCase):
    """``occupancy_holder`` and the CAS ``grantable`` predicate answer one question.

    They must be exact complements on every row state, because they drive opposite
    halves of the gate: ``grantable`` decides who gets the tree, ``occupancy_holder``
    decides whom the refusal path and the doctor report as holding it. A state where
    both say "free" is fine; one where the CAS grants while the reporter still names a
    holder refuses ``workspace ticket`` forever over a row every acquire would win —
    the never-lockout failure, reachable from any write that lands one column late.
    """

    def rival_wins(self) -> bool:
        try:
            acquire(self.fresh(), holder="task:rival", holder_session="s-rival")
        except WorktreeOccupiedError:
            return False
        return True

    def assert_complementary(self) -> None:
        held = occupancy_holder(self.fresh()) is not None
        assert held is not self.rival_wins()

    def test_an_unheld_row_is_grantable_and_reports_no_holder(self) -> None:
        self.assert_complementary()

    def test_a_live_claim_is_ungrantable_and_reports_its_holder(self) -> None:
        acquire(self.worktree, holder="task:1", holder_session="s1", lease_seconds=600)
        self.assert_complementary()

    def test_a_lapsed_claim_is_grantable_and_reports_no_holder(self) -> None:
        acquire(self.worktree, holder="task:1", holder_session="s1", lease_seconds=60)
        Worktree.objects.filter(pk=self.worktree.pk).update(occupancy_expires_at=timezone.now() - timedelta(seconds=1))
        self.assert_complementary()

    def test_a_claim_with_no_expiry_is_grantable_and_reports_no_holder(self) -> None:
        acquire(self.worktree, holder="task:1", holder_session="s1")
        Worktree.objects.filter(pk=self.worktree.pk).update(occupancy_expires_at=None)
        self.assert_complementary()


class HolderIdTests(TestCase):
    def test_the_task_holder_id_is_stable_and_qualified(self) -> None:
        task = TaskFactory()
        assert task_holder_id(task) == f"task:{task.pk}"
